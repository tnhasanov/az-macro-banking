"""Optional sector review (8 slides) for a configured sector: agriculture, construction, trade, transport, industry."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .. import config
from ..facts import FactPackBuilder
from ..narrative.fmt import money, num, plabel
from ..storage.db import Database, utcnow
from ..util.periods import parse_as_of
from .builder import Deck, month_labels
from .monthly import _chg, _fmt, _kpi, _per
from .pdf import convert_to_pdf, previews, soffice_available


def generate_sector(as_of: str | None, sector: str, lang: str, force: bool = False, db: Database | None = None) -> dict[str, Any]:
    paths = config.paths()
    paths.ensure()
    own = db is None
    db = db or Database(paths.db_path)
    scfg = config.reports_config()["sector"]["sectors"].get(sector)
    if not scfg:
        raise ValueError(f"unknown sector {sector}; configured: {list(config.reports_config()['sector']['sectors'])}")
    as_of_d = parse_as_of(as_of)
    b = FactPackBuilder(db, as_of_d, lang=lang)
    fp = b.build()
    fp["report_type"] = "sector"
    theme = dict(config.theme())
    theme["_root"] = str(config.ROOT)
    d = Deck(theme, lang)
    C = theme["colors"]
    label = scfg["label"]
    credit = scfg["cba_credit"]
    biz = scfg.get("cba_bank_business")
    act_ids = scfg["ssc_activity"]
    src = f"Source: CBA Table 2.8 (sector loans), Table 5.8 (bank business portfolio) and SSC headline/monthly report; cutoff {fp['as_of']} (Asia/Baku)."

    credit_lvl = b.snap(credit, compare="lag12", key="sector.credit.level")
    credit_yoy = b.snap(credit + ".yoy", compare="lag1", key="sector.credit.yoy")
    credit_share = b.snap(credit + ".share", compare="lag12", key="sector.credit.share")
    acts = [b.snap(a, compare="prior_edition", key=f"sector.activity.{i}") for i, a in enumerate(act_ids)]
    biz_lvl = b.snap(biz, compare="lag12", key="sector.biz.level") if biz else None
    gdp_share_id = {"agriculture": "ssc.gdp.sector.agriculture.share_nominal", "construction": "ssc.gdp.sector.construction.share_nominal", "trade": "ssc.gdp.sector.trade.share_nominal",
                    "transport": "ssc.gdp.sector.transport.share_nominal", "industry": "ssc.gdp.sector.industry.share_nominal"}.get(sector)
    gdp_share = b.snap(gdp_share_id, compare="lag12", key="sector.gdp.share") if gdp_share_id else None

    # S01 cover / scale
    s = d.new_slide()
    d.add_rect(s, 0, 0, 4.85, 7.5, C["primary"], None, radius=None)
    d.add_rect(s, 4.85, 0, 0.09, 7.5, C["gold"], None, radius=None)
    d.add_text(s, 0.56, 1.1, 4.0, 0.3, config.term("sector_review", lang).upper(), size=9, color="DDD0EA", bold=True)
    d.add_text(s, 0.56, 1.5, 4.1, 1.4, f"{label}: activity, credit and banking questions", size=24, bold=True, color=C["white"])
    d.add_text(s, 0.56, 3.0, 4.0, 1.0, f"As of {fp['as_of']} · {config.term('draft_label', lang)} · banking data to {plabel(fp['edition'].get('banking_period'), 'month_end_stock')}", size=11, color="DDD0EA")
    d.add_text(s, 0.6, 6.62, 4.0, 0.3, f"{config.settings()['report'].get('organisation_label')}  |  {config.settings()['report'].get('audience_label')}", size=9, color="DDD0EA")
    if d.logo_path:
        s.shapes.add_picture(d.logo_path, d.prs.slide_width - d.prs.slide_width * 0.46, d.prs.slide_height * 0.15, width=d.prs.slide_width * 0.16)
    tiles = [_kpi(gdp_share, f"{label}: share of nominal GDP (YTD)") if gdp_share and gdp_share.get("latest") else ("n/a", "Share of GDP", "not published for this mapping"),
             _kpi(credit_lvl, f"Loans to {label.lower()} (all CI)"), _kpi(credit_share, "Share of real-sector loans"), _kpi(biz_lvl, "Bank business portfolio, sector") if biz_lvl and biz_lvl.get("latest") else ("n/a", "Bank business portfolio", "not published")]
    for i, (v, l, sub) in enumerate(tiles):
        col, row = i % 2, i // 2
        d.add_kpi(s, 5.6 + col * 3.6, 2.6 + row * 1.25, 3.4, 1.1, v, l, sub)
    d.add_notes(s, "Sector scale uses nominal GDP share (SSC Table 2, YTD) and the CBA sectoral loan table 2.8 share of real-sector loans.")

    def chart_slide(title: str, section: str, desc: str, series: list[tuple[str, dict | None, str]], tiles: list[tuple[str, str, str]], so_what: str, notes: str, number_format="0.0"):
        s = d.new_slide()
        d.add_title(s, title, section, desc)
        cats_all, ser_all = None, []
        for sid, dims, lbl in series:
            cs = b.chart_series(sid, dims, window=36, label=lbl)
            if cs["points"]:
                ser_all.append(cs)
        if ser_all:
            from .builder import align_series

            periods, values = align_series(ser_all)
            cats = month_labels(periods, lang)
            colors = [C["primary"], C["positive"], C["gold"], C["neutral"]]
            d.add_line_chart(s, 0.45, 1.5, 7.9, 5.1, cats, [{"name": cs["label"], "values": v, "color": colors[i % 4]} for i, (cs, v) in enumerate(zip(ser_all, values))], number_format=number_format)
        else:
            d.add_text(s, 0.45, 1.6, 7.9, 1.0, "No compatible published series available for this panel.", size=11, color=C["muted"])
        for i, (v, l, sub) in enumerate(tiles[:4]):
            col, row = i % 2, i // 2
            d.add_kpi(s, 8.5 + col * 2.25, 1.45 + row * 1.02, 2.15, 0.92, v, l, sub)
        d.add_so_what(s, 8.5, 4.2, 4.4, 2.4, so_what, heading="NOTE")
        d.add_footer(s, src, d.page)
        d.add_notes(s, notes)

    act_series = [(a, None, a.split(".")[-2].replace("_", " ") + " " + a.split(".")[-1]) for a in act_ids]
    chart_slide(f"{label}: activity trends", "Activity", "SSC real growth measures, YTD y/y by edition (%)", act_series, [_kpi(a, a.get("label", "")[:36]) for a in acts if a.get("latest")],
                "YTD growth is cumulative; the measures listed are output, value added, turnover or investment as labelled and are not interchangeable.", "SSC headline table and monthly report Table 2.")
    chart_slide(f"{label}: prices and costs (available indicators)", "Prices", "Only CPI aggregates are collected in version one; sector producer prices are listed in the SSC monthly report (Table 41) but not parsed",
                [("ssc.cpi.all.yoy", None, "CPI y/y"), ("ssc.cpi.food.yoy", None, "Food CPI y/y")], [_kpi(fp["metrics"].get("ssc.cpi.all.yoy"), "CPI y/y"), _kpi(fp["metrics"].get("ssc.cpi.food.yoy"), "Food CPI y/y")],
                "Producer price indices for agriculture, industry and construction are published by the SSC and can be added to sources.yaml; they are not part of this edition.", "Limitation recorded in the availability matrix.")
    chart_slide(f"{label}: investment context", "Investment", "SSC fixed capital investment (economy-wide and non-oil), real YTD y/y; sector-specific investment not collected",
                [("ssc.hl.investment.growth", None, "Fixed capital investment, YTD y/y"), ("ssc.hl.investment_nonoil.growth", None, "Non-oil investment, YTD y/y")],
                [_kpi(fp["metrics"].get("ssc.hl.investment.growth"), "Investment, YTD y/y"), _kpi(fp["metrics"].get("ssc.hl.investment_nonoil.growth"), "Non-oil investment")],
                "Investment is not construction output; sector-level investment tables exist in SSC open data and are a future extension.", "SSC headline table.")
    chart_slide(f"{label}: external trade context", "External trade", "Economy-wide exports and non-oil exports (SSC, YTD); sector-specific trade requires the SSC foreign-trade tables",
                [("ssc.hl.exports_nonoil.growth", None, "Non-oil exports, YTD y/y"), ("ssc.hl.imports.growth", None, "Imports, YTD y/y")],
                [_kpi(fp["metrics"].get("ssc.hl.exports_nonoil.level"), "Non-oil exports, YTD"), _kpi(fp["metrics"].get("ssc.hl.imports.level"), "Imports, YTD")],
                "Merchandise trade only; not the current account.", "SSC headline table.", number_format="0.0")
    chart_slide(f"{label}: credit growth", "Credit", "CBA Table 2.8 sector loans (all credit institutions, nominal stock) and bank business portfolio (Table 5.8, prudential)",
                [(credit + ".yoy", None, "Sector loans y/y (all CI)")] + ([(biz + ".yoy", None, "Bank business portfolio y/y")] if biz and b.eng.get(biz + ".yoy") else []),
                [_kpi(credit_lvl, "Sector loans"), _kpi(credit_yoy, "Sector loans y/y"), _kpi(credit_share, "Share of real-sector loans")],
                "Stock changes combine originations, repayments, write-offs and reclassifications; new-lending by sector is not published in the collected tables.", "CBA 2.8 and 5.8.")
    # S07 credit vs activity + regional note
    s = d.new_slide()
    d.add_title(s, f"{label}: credit versus activity", "Comparison", "Nominal loan-stock growth (CBA) vs real activity growth (SSC); regional loan tables are not sector-specific")
    rows = [["Sector loans y/y (nominal, all CI)", _fmt(credit_yoy), _per(credit_yoy), _chg(credit_yoy)]]
    for a in acts:
        if a.get("latest"):
            rows.append([a.get("label", "")[:60], _fmt(a), _per(a), _chg(a)])
    d.add_table(s, 0.45, 1.5, 7.9, 2.5, ["Measure", "Latest", "Period", "Change"], rows, col_widths=[4.0, 1.2, 1.5, 1.4], font_size=9.5, align=["l", "r", "l", "r"])
    d.add_so_what(s, 8.5, 1.5, 4.4, 2.5, "A gap between nominal credit growth and real activity growth reflects inflation, starting penetration, seasonality and mapping differences before any lending signal. Ask sector specialists which explanation applies.", heading="READING")
    d.add_text(s, 0.45, 4.3, 12.35, 0.3, "Regional pattern: CBA regional loan tables (2.10) are published by booking region without sector detail; a sector-by-region view is not available from public data.", size=10, color=C["muted"])
    d.add_footer(s, src, d.page)
    # policy and stability conditions that bear on this sector
    from ..publications.factpack import PublicationFacts

    pf = PublicationFacts(db, as_of_d, lang)
    pol, stab = pf.policy_block(), pf.stability_block()
    decision = pol.get("decision") or {}
    car = next((r for r in stab.get("dashboard") or [] if r["series_id"] == "cba.fsr.car"), None)
    s = d.new_slide()
    d.add_title(s, f"{label}: policy and stability conditions around this sector", "Policy and stability",
                "Public conditions that bear on lending to this sector; none of it is sector-specific supervisory data")
    rows = [["Refinancing rate and corridor",
             f"{decision.get('policy_rate')}% (corridor {decision.get('corridor_floor')}%–{decision.get('corridor_ceiling')}%)",
             f"decided {decision.get('announcement_date')}",
             "sets the floor under manat funding costs for loans written to this sector"],
            ["Policy stance versus the previous decision", pol["stance"].get("label") or "not established",
             pol["stance"].get("statement", "")[:90],
             "a change in stance reaches sector lending through pricing and credit supply"]]
    infl = next((x for x in pol["forecasts"].get("current") or [] if x["series_id"] == "cba.forecast.inflation"), None)
    if infl:
        rows.append(["CBA inflation projection", f"{infl['value']}% for {infl.get('horizon')}",
                     f"{pol['forecasts'].get('current_vintage')} round",
                     "shapes nominal turnover in the sector and the real burden of existing debt"])
    if car:
        rows.append(["Sector capital adequacy (regulatory)", f"{car['value']}%",
                     f"{car['observation_date']}, published {car['publication']['published_at']}",
                     "system headroom conditions how much risk the sector's lenders can take on"])
    d.add_table(s, 0.45, 1.45, 12.35, 2.8, ["Condition", "Latest", "Date", "How it bears on this sector"], rows,
                col_widths=[3.0, 2.6, 2.6, 4.15], font_size=8.5)
    d.add_text(s, 0.45, 4.45, 12.35, 1.9,
               [[{"text": "What this does and does not say: ", "bold": True, "size": 10},
                 {"text": "policy and stability publications are system-wide. They carry no breakdown by sector and no "
                          "bank-level detail, so the rows above are conditions surrounding this sector's credit, not "
                          "measurements of it. A sector conclusion still rests on the loan and activity data on the "
                          "previous slides.", "size": 10}]],
               size=10, color=C["text"], autofit=True)
    d.add_footer(s, src, d.page)
    d.add_notes(s, f"Policy source: decision of {decision.get('announcement_date')}; stability source: "
                   f"{(stab.get('report') or {}).get('edition')} published {(stab.get('report') or {}).get('published_at')}.")
    # S08 questions + sources
    s = d.new_slide()
    d.add_title(s, f"{label}: banking questions and sources", "Questions", "Proportionate follow-ups: investigate, compare, monitor")
    qs = [f"How does the bank's own {label.lower()} exposure growth compare with the sector's published loan growth and activity growth?",
          f"Which sub-segments of {label.lower()} explain the published activity trend, and are they represented in the portfolio?",
          "What internal early-warning indicators (arrears, restructurings, utilisation) should be reviewed alongside the next CBA release?"]
    d.add_text(s, 0.45, 1.5, 7.9, 3.0, [[{"text": q, "size": 11}] for q in qs], bullets=True, space_after=8)
    docs = [x for x in fp["sources"] if x["dataset_id"] in ("cba_loans_by_sector", "cba_bank_business_sectors", "ssc_macro_headline_html", "ssc_monthly_report")][-6:]
    rows = [[x["dataset_id"], (x["title_original"] or "")[:50], x.get("published_at") or "n/a"] for x in docs]
    d.add_table(s, 8.5, 1.5, 4.4, 3.0, ["Dataset", "Document", "Published"], rows, col_widths=[1.6, 2.0, 0.8], font_size=7.5, align=["l", "l", "l"])
    d.add_footer(s, src, d.page)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    edition = f"{sector}_{as_of_d.isoformat()}"
    version = len([e for e in db.editions("sector") if e["edition_period"] == edition]) + 1
    out = paths.output_dir / "sector" / edition / f"v{version}_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    pptx_path = d.save(out / f"AZ_Sector_Review_{sector}_{as_of_d.isoformat()}_v{version}.pptx")
    pdf_info: dict[str, Any] = {"status": "skipped"}
    if soffice_available():
        try:
            pdf = convert_to_pdf(pptx_path, out)
            pdf_info = {"status": "ok", "path": str(pdf)}
            previews(pdf, out / "previews")
        except Exception as exc:
            pdf_info = {"status": "failed", "reason": str(exc)}
    manifest = {"report_type": "sector", "sector": sector, "edition": edition, "version": version, "generated_at": utcnow(), "as_of": as_of_d.isoformat(), "files": {"pptx": str(pptx_path), "pdf": pdf_info},
                "fact_pack_hash": fp.get("fact_pack_hash"), "n_slides": d.page}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out / "fact_pack.json").write_text(json.dumps(fp, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    db.add_edition({"edition_id": f"sector:{edition}:v{version}", "report_type": "sector", "edition_period": edition, "version": version, "generated_at": manifest["generated_at"], "as_of": as_of_d.isoformat(),
                    "snapshot_id": None, "status": "generated", "path": str(out), "manifest_path": str(out / "manifest.json"), "anchors": json.dumps({"sector": sector})})
    if own:
        db.close()
    return {"status": "generated", "path": str(out), "pptx": str(pptx_path), "pdf": pdf_info, "n_slides": d.page, "sector": sector}
