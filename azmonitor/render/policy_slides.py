"""Policy and stability slides for the monthly deck, and their appendices.

Four rules are visible in the layout rather than only in the notes, because a slide is read on its
own:

* a stability row shows its own reporting date next to the date its report was published, so an
  older measure is never mistaken for this month's;
* regulatory capital adequacy and the book capital ratio appear in separate blocks with a line
  saying they measure different things;
* stress-test results are labelled with their scenario and the exercise they come from, and the
  word forecast is not used for them;
* a projection is labelled as the Central Bank's, with the round it was published in.
"""
from __future__ import annotations

import re

import datetime as dt
from typing import Any

from ..narrative.fmt import num, plabel

BAND = "Interest rate corridor"


def _month(value: str | None) -> str:
    """A release date shortened to its month, for the small print on a KPI card."""
    if not value:
        return "n/a"
    try:
        return dt.date.fromisoformat(value).strftime("%b %Y")
    except ValueError:
        return value


def _d(value: str | None) -> str:
    """A date as printed on a slide."""
    if not value:
        return "n/a"
    try:
        return dt.date.fromisoformat(value).strftime("%d %b %Y")
    except ValueError:
        return value


def _pct(value: float | None, dec: int = 2) -> str:
    return f"{value:.{dec}f}%" if isinstance(value, (int, float)) else "n/a"


class PolicyStabilitySlides:
    """Mixin for MonthlyRenderer: slides M19-M22 and appendices A05-A06."""

    # ------------------------------------------------------------------ M19
    def m19(self):
        f = self.fp["slides"]["M19"]
        d, C = self.d, self.C
        decision = f.get("decision") or {}
        stance = f.get("stance") or {}
        corridor = f.get("corridor_now") or {}
        bp = stance.get("rate_change_bp")
        kpis = [
            (_pct(corridor.get("rate")), "Refinancing rate", f"decided {_d(decision.get('announcement_date'))}"),
            (f"{_pct(corridor.get('floor'))} – {_pct(corridor.get('ceiling'))}", "Corridor floor and ceiling",
             f"width {corridor.get('width_pp')} pp" if corridor.get("width_pp") is not None else ""),
            (f"{bp:+.0f} bp" if isinstance(bp, (int, float)) else "n/a", "Change at this meeting",
             stance.get("label") or ""),
            (_d((f.get("next_decision") or {}).get("date")), "Next decision",
             (f.get("next_decision") or {}).get("status") or ""),
        ]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w, "Refinancing rate and the interest rate corridor at each decision (%)")
            path = f.get("rate_path") or []
            floor = {p["date"]: p["value"] for p in (f.get("corridor_path") or {}).get("floor", [])}
            ceil = {p["date"]: p["value"] for p in (f.get("corridor_path") or {}).get("ceiling", [])}
            cats = [_d(p["date"]) for p in path]
            series = [
                {"name": "Corridor ceiling", "values": [ceil.get(p["date"]) for p in path], "color": self.SC.get("fx", "#B39DDB")},
                {"name": "Refinancing rate", "values": [p["value"] for p in path], "color": self.SC.get("total", "#6F00B6")},
                {"name": "Corridor floor", "values": [floor.get(p["date"]) for p in path], "color": self.SC.get("deposits", "#00897B")},
            ]
            d.add_line_chart(s, x, y + 0.25, w, h * 0.55, cats, series, number_format="0.00", skip=max(1, len(cats) // 8))
            rows = [[r["dimension"], str(r["previous"])[:230], str(r["current"])[:230], str(r.get("evidence") or "")[:60],
                     str(r.get("implication") or "")[:70]] for r in (f.get("comparison") or [])]
            self._caption(s, x, y + h * 0.55 + 0.35, w,
                          "Previous assessment → current assessment → evidence → what it bears on for a bank")
            d.add_table(s, x, y + h * 0.55 + 0.6, w, h * 0.45 - 0.6,
                        ["", "Previous", "Current", "Evidence", "Bears on"], rows,
                        col_widths=[1.0, 2.35, 2.35, 1.0, 1.05], font_size=6.5, align=["l", "l", "l", "l", "l"])

        review = f.get("review") or {}
        self._standard("M19", "Monetary policy stance: the latest decision and what changed",
                       "Monetary policy", f"Decision of {_d(decision.get('announcement_date'))}; rates from the decision "
                                          f"table in the {review.get('edition') or 'Monetary Policy Review'}",
                       kpis, main,
                       [f"CBA monetary policy decision, {_d(decision.get('announcement_date'))} "
                        f"({decision.get('rationale_language') or 'az'} edition); CBA {review.get('edition') or 'Monetary Policy Review'} decision table"],
                       [], ["cba.policy.rate", "cba.policy.corridor_floor", "cba.policy.corridor_ceiling"],
                       so_what_default=stance.get("statement") or "",
                       extra_notes=self._policy_notes(f))

    def _policy_notes(self, f: dict[str, Any]) -> str:
        decision = f.get("decision") or {}
        pub = (decision.get("publication") or {})
        docs = "\n".join(f"- {d.get('language')}: {d.get('url')} (sha256 {d.get('sha256')}, retrieved {str(d.get('retrieved_at'))[:10]})"
                         for d in (pub.get("documents") or []))
        return (f"Decision announced {decision.get('announcement_date')}; effective date "
                f"{decision.get('effective_date') or 'not stated'} ({decision.get('effective_date_basis')}).\n"
                f"Stance label describes the rate decision only: {f.get('stance', {}).get('label')}.\n"
                f"Rationale, verbatim extract: {(decision.get('rationale') or '')[:900]}\n\nDecision documents:\n{docs}\n\n"
                f"{f.get('note', '')}")

    # ------------------------------------------------------------------ M20
    def m20(self):
        f = self.fp["slides"]["M20"]
        d = self.d
        cpi = f.get("cpi") or {}
        forecasts = f.get("forecasts") or []
        by_horizon = {x.get("horizon"): x for x in forecasts if x["series_id"] == "cba.forecast.inflation"}
        eoy = next((v for k, v in by_horizon.items() if k and k.startswith("end-")), None)
        kpis = [
            (_pct((cpi.get("latest") or {}).get("value"), 1), "CPI inflation, actual",
             plabel((cpi.get("latest") or {}).get("period"), cpi.get("period_type"))),
        ]
        # the horizon rides on the sub-line with the vintage: label and sub then take one line each,
        # and neither runs past the bottom of the card
        for horizon, entry in list(by_horizon.items())[:3]:
            short = re.sub(r"\s*\([^)]*\)", "", horizon or "")   # the full horizon label is in the table beside it
            kpis.append((_pct(entry["value"], 1), "CBA inflation projection",
                         f"{short}, {f.get('current_vintage')} round"))
        growth = [x for x in forecasts if x["series_id"].startswith("cba.forecast.gdp")]
        if growth:
            kind = "non-oil GDP" if "nonoil" in growth[0]["series_id"] else "GDP"
            kpis.append((_pct(growth[0]["value"], 1), f"CBA {kind} growth",
                         f"{growth[0].get('horizon')}, {f.get('current_vintage')} round"))

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.55, "Actual CPI inflation, y/y (%) — outcomes only")
            series = self.fp["slides"]["M05"].get("chart_cpi") or []
            if series:
                cats, ser = self._chart_series(series[:1], [self.SC.get("cpi", "#E53935")])
                d.add_line_chart(s, x, y + 0.25, w * 0.55 - 0.1, h * 0.55, cats, ser, number_format="0.0")
            self._caption(s, x + w * 0.55, y, w * 0.45, f"CBA projections, {f.get('current_vintage')} round")
            rows = [[x_["label"] or x_["series_id"], x_.get("horizon") or "", _pct(x_["value"], 1),
                     (x_.get("scenario") or "baseline")] for x_ in forecasts]
            d.add_table(s, x + w * 0.55, y + 0.25, w * 0.45, h * 0.55, ["Projection", "Target", "Value", "Scenario"],
                        rows, col_widths=[2.0, 1.3, 0.7, 0.9], font_size=8)
            revisions = [r for r in (f.get("revisions") or []) if r.get("comparable")]
            self._caption(s, x, y + h * 0.55 + 0.35, w,
                          f"Revisions against the {f.get('previous_vintage')} round, matched on the same target period")
            if revisions:
                rrows = [[r["series_id"].split(".")[-1], r["target_period"], _pct(r["previous"], 1), _pct(r["current"], 1),
                          f"{r['revision_pp']:+.1f} pp" if r.get("revision_pp") is not None else "n/a"] for r in revisions]
                d.add_table(s, x, y + h * 0.55 + 0.6, w, h * 0.45 - 0.6,
                            ["Projection", "Target period", f"{f.get('previous_vintage')}", f"{f.get('current_vintage')}", "Revision"],
                            rrows, col_widths=[2.2, 1.6, 1.2, 1.2, 1.4], font_size=8)
            else:
                d.add_text(s, x, y + h * 0.55 + 0.62, w, 0.5,
                           "No target period is published in both rounds, so no like-for-like revision can be shown. "
                           "Targets published in only one round are listed above with their round.",
                           size=9, color=self.C["muted"])

        risks = f.get("risks") or []
        so_what = (f"The Central Bank states: {risks[0]['text']}" if risks else
                   "The Central Bank's projections are shown as published; no projection of our own is implied.")
        self._standard("M20", "Inflation outlook: what the Central Bank projects and how it has changed",
                       "Inflation outlook",
                       f"Actual CPI from SSC; projections from the CBA {f.get('current_vintage')} round, compared with {f.get('previous_vintage')}",
                       kpis, main,
                       ["SSC consumer price statistics (actual); CBA Monetary Policy Review and decision statement (projections)"],
                       ["ssc_price_bulletin"], ["ssc.cpi.all.yoy"],
                       so_what_default=so_what,
                       extra_notes="Projections are the Central Bank's own, tagged with the round they were published in. "
                                   "They are not outcomes and not our scenarios. A revision is only shown where the same "
                                   "target period appears in both rounds.\n\n"
                                   + "\n".join(f"- risk stated by the CBA: {r['text']}" for r in risks))

    # ------------------------------------------------------------------ M21
    def m21(self):
        f = self.fp["slides"]["M21"]
        d = self.d
        dash = {r["series_id"]: r for r in f.get("dashboard") or []}
        report = f.get("report") or {}

        def kpi_for(series_id: str, label: str) -> tuple[str, str, str]:
            row = dash.get(series_id)
            if not row:
                return ("not published", label, "not in the collected reports")
            # the day of the release is in the table beside this card and in the subtitle; the card
            # keeps the month so the sub-line stays on one line and does not print past the panel
            return (_pct(row["value"], 1), label,
                    f"{plabel(row['observation_date'], 'month_end_stock')} · published {_month(row['publication']['published_at'])}")

        kpis = [kpi_for("cba.fsr.car", "Capital adequacy (regulatory)"), kpi_for("cba.fsr.lcr", "Liquidity coverage ratio"),
                kpi_for("cba.fsr.npl_ratio", "NPL ratio (stability report)"), kpi_for("cba.fsr.roe", "Return on equity"),
                (_pct((f.get("book_measures", {}).get("equity_to_assets", {}).get("latest") or {}).get("value"), 1),
                 "Capital / assets (book)", plabel((f.get("book_measures", {}).get("equity_to_assets", {}).get("latest") or {}).get("period"), "month_end_stock")),
                (_pct((f.get("book_measures", {}).get("liquid_assets_ratio", {}).get("latest") or {}).get("value"), 1),
                 "Liquid assets / assets (book)", plabel((f.get("book_measures", {}).get("liquid_assets_ratio", {}).get("latest") or {}).get("period"), "month_end_stock"))]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w, "Regulatory measures from the Financial Stability Report, each at its own reporting date")
            rows = []
            for r in f.get("dashboard") or []:
                prev = r.get("previous") or {}
                change = (f"{r['change']:+.1f} pp" if isinstance(r.get("change"), (int, float))
                          else ("not consecutive" if r.get("change_note") else "n/a"))
                rows.append([
                    (r.get("label") or r["series_id"])[:44],
                    _pct(r["value"], 1),
                    plabel(r["observation_date"], "month_end_stock"),
                    _d(r["publication"]["published_at"]),
                    (f"{_pct(prev.get('value'), 1)} ({plabel(prev.get('observation_date'), 'month_end_stock')})" if prev else "n/a"),
                    change,
                ])
            d.add_table(s, x, y + 0.25, w, h * 0.52, ["Indicator", "Value", "Reporting date", "Published", "Previous reading", "Change"],
                        rows, col_widths=[2.6, 0.75, 1.15, 1.0, 1.35, 0.9], font_size=7.5)
            self._caption(s, x, y + h * 0.52 + 0.35, w, "Book measures from the monthly balance sheet — a different concept, shown separately")
            d.add_text(s, x, y + h * 0.52 + 0.6, w, h * 0.48 - 0.6,
                       [[{"text": f.get("distinction", ""), "size": 9}],
                        [{"text": f.get("as_of_note", ""), "size": 9}]] +
                       [[{"text": f"{rec['concept']}: ", "bold": True, "size": 9},
                         {"text": f"{(rec['primary'] or {}).get('series_id')} = "
                                  f"{num((rec['primary'] or {}).get('value'), 1, '%')} at {(rec['primary'] or {}).get('period')}; "
                                  f"{(rec['secondary'] or {}).get('series_id')} = "
                                  f"{num((rec['secondary'] or {}).get('value'), 1, '%')} at {(rec['secondary'] or {}).get('period')}. "
                                  f"{rec['note'][:1].upper()}{rec['note'][1:]}.", "size": 8.5}]
                        for rec in (f.get("reconciliation") or [])],
                       size=9, color=self.C["text"], space_after=3, autofit=True)

        self._standard("M21", "Financial stability dashboard: capital, liquidity and asset quality",
                       "Financial stability",
                       f"{report.get('type', 'Financial Stability Report')} {report.get('edition') or ''}, reporting period "
                       f"{report.get('reporting_period_end') or 'n/a'}, published {_d(report.get('published_at'))}",
                       kpis, main,
                       [f"CBA {report.get('type', 'Financial Stability Report')} {report.get('edition') or ''} "
                        f"(reporting period {report.get('reporting_period_end')}, published {report.get('published_at') or 'date unknown'}); "
                        f"CBA Table 5.2 for the book ratios"],
                       ["cba_bank_balance"], ["cba.bank.equity_to_assets", "cba.bank.liquid_assets_ratio"],
                       so_what_default="Regulatory capital and liquidity are published half-yearly and are older than "
                                       "this month's banking data; both dates are shown on every row.",
                       extra_notes=self._stability_notes(f))

    def _stability_notes(self, f: dict[str, Any]) -> str:
        report = f.get("report") or {}
        docs = "\n".join(f"- {d.get('language')}: {d.get('url')} (sha256 {d.get('sha256')}, {d.get('pages')} pages)"
                         for d in (report.get("documents") or []))
        cites = "\n".join(f"- {r.get('label')}: {r['value']}{r['unit']} at {r['observation_date']}, {r.get('source_citation')}, "
                          f"definition: {r.get('definition')}" for r in (f.get("dashboard") or []))
        return (f"Source publication: {report.get('type')} {report.get('edition')}, reporting period "
                f"{report.get('reporting_period_start')} to {report.get('reporting_period_end')}, published "
                f"{report.get('published_at')} ({report.get('published_at_basis')}).\n\nPage-level citations:\n{cites}\n\n"
                f"Documents:\n{docs}\n\n{f.get('distinction', '')}")

    # ------------------------------------------------------------------ M22
    def m22(self):
        f = self.fp["slides"]["M22"]
        d = self.d
        stress = f.get("stress") or []
        years = sorted({x["observation_date"][:4] for x in stress})
        scenarios = sorted({x.get("scenario") for x in stress if x.get("scenario")})
        funding = f.get("funding") or {}
        kpis = []
        for sc in scenarios[:2]:
            vals = [x for x in stress if x.get("scenario") == sc]
            if vals:
                last = sorted(vals, key=lambda v: v["observation_date"])[-1]
                kpis.append((_pct(last["value"], 1), f"Stress CAR, {sc} scenario", f"projection for {last['observation_date'][:4]}"))
        kpis.append((_pct((funding.get("ldr", {}).get("latest") or {}).get("value"), 1), "Loan-to-deposit ratio",
                     plabel((funding.get("ldr", {}).get("latest") or {}).get("period"), "month_end_stock")))
        kpis.append((_pct((funding.get("deposit_fx_share", {}).get("latest") or {}).get("value"), 1), "FX share of deposits",
                     plabel((funding.get("deposit_fx_share", {}).get("latest") or {}).get("period"), "month_end_stock")))

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.5,
                          f"Stress-test projection of regulatory capital adequacy (%), exercise from {f.get('exercise') or 'n/a'}")
            if stress and years:
                series = []
                colours = {"baseline": self.SC.get("total", "#6F00B6"), "adverse": self.SC.get("overdue", "#E53935")}
                for i, sc in enumerate(scenarios):
                    values = []
                    for yr in years:
                        hit = next((v for v in stress if v.get("scenario") == sc and v["observation_date"][:4] == yr), None)
                        values.append(hit["value"] if hit else None)
                    series.append({"name": f"{sc} scenario", "values": values,
                                   "color": colours.get(sc, self.SC.get("other", "#9E9E9E"))})
                d.add_bar_chart(s, x, y + 0.25, w * 0.5 - 0.1, h * 0.55, years, series, number_format="0.0",
                                data_labels=True, gap_width=60)
            self._caption(s, x + w * 0.5, y, w * 0.5, "Published concentrations and funding structure")
            rows = [[c["label"][:46], num((c["snapshot"].get("latest") or {}).get("value"), 1, "%"),
                     plabel((c["snapshot"].get("latest") or {}).get("period"), "month_end_stock")]
                    for c in (f.get("concentration") or [])]
            for key, label in (("ldr", "Loan-to-deposit ratio"), ("deposit_fx_share", "FX share of deposits"),
                               ("corporate_deposits_yoy", "Corporate deposits, y/y")):
                snap = funding.get(key) or {}
                if (snap.get("latest") or {}).get("value") is not None:
                    rows.append([label, num(snap["latest"]["value"], 1, "%"), plabel(snap["latest"]["period"], snap.get("period_type"))])
            d.add_table(s, x + w * 0.5, y + 0.25, w * 0.5, h * 0.55, ["Measure", "Latest", "Reporting date"], rows,
                        col_widths=[2.6, 0.9, 1.4], font_size=8)
            self._caption(s, x, y + h * 0.55 + 0.35, w, "Scenarios as described by the Central Bank")
            d.add_text(s, x, y + h * 0.55 + 0.6, w, h * 0.45 - 0.6,
                       [[{"text": f.get("note") or "", "size": 9}]] +
                       [[{"text": f"{sc}: ", "bold": True, "size": 9},
                         {"text": next((x_.get("definition") or "" for x_ in stress if x_.get("scenario") == sc), "")[:230],
                          "size": 8.5}] for sc in scenarios],
                       size=9, color=self.C["text"], space_after=3, autofit=True)

        self._standard("M22", "Systemic vulnerabilities: concentrations, funding and stress-test findings",
                       "Vulnerabilities",
                       f"Stress results from the Financial Stability Report exercise starting {f.get('exercise') or 'n/a'}; "
                       f"concentrations from the monthly CBA tables",
                       kpis, main,
                       ["CBA Financial Stability Report (stress tests and scenarios); CBA Tables 2.8, 2.11, 2.12 for "
                        "concentration and funding structure"],
                       ["cba_loans_by_sector", "cba_deposits", "cba_deposits_currency"],
                       ["cba.ldr", "cba.deposits.fx_share"],
                       so_what_default="Stress results are projections under the Central Bank's published scenarios, not "
                                       "forecasts and not outcomes.",
                       extra_notes="Each stress figure carries its scenario and the exercise date it starts from. "
                                   "Concentration measures are sector shares of the published loan stock and say nothing "
                                   "about any individual bank's book.")

    # ------------------------------------------------------------------ A05/A06
    def a05(self):
        f = self.fp["slides"]["A05"]
        d = self.d
        s = d.new_slide()
        d.add_title(s, "A05 · Policy decisions, projections and stress results", "Appendix",
                    "Every decision and projection behind slides M19 to M22, with the publication each came from")
        rows = [[_d(p["date"]), _pct(p["value"])] for p in (f.get("decisions") or [])][-14:]
        d.add_text(s, 0.45, 1.45, 4.0, 0.25, "Refinancing rate by decision date", size=9, bold=True, color=self.C["muted"])
        d.add_table(s, 0.45, 1.72, 4.0, 4.6, ["Decision date", "Rate"], rows, col_widths=[2.3, 1.7], font_size=8)
        fc = f.get("forecasts") or {}
        frows = [[x["series_id"].split(".")[-1], x.get("horizon") or "", x["observation_date"], _pct(x["value"], 1),
                  x.get("scenario") or "baseline", x.get("publication", {}).get("label") or ""] for x in (fc.get("current") or [])]
        d.add_text(s, 4.7, 1.45, 4.2, 0.25, f"CBA projections, {fc.get('current_vintage')} round", size=9, bold=True, color=self.C["muted"])
        d.add_table(s, 4.7, 1.72, 4.2, 2.2, ["Projection", "Horizon", "Target", "Value", "Scenario", "Edition"], frows,
                    col_widths=[0.9, 0.85, 0.75, 0.45, 0.65, 0.6], font_size=7)
        st = (f.get("stress") or {}).get("results") or []
        srows = [[x.get("scenario") or "", x["observation_date"], _pct(x["value"], 1), (x.get("dims") or {}).get("exercise", "")]
                 for x in st]
        d.add_text(s, 4.7, 4.05, 4.2, 0.25, "Stress-test projections (not forecasts)", size=9, bold=True, color=self.C["muted"])
        d.add_table(s, 4.7, 4.32, 4.2, 2.0, ["Scenario", "Target", "CAR", "Exercise"], srows,
                    col_widths=[1.1, 1.1, 0.8, 1.2], font_size=7.5)
        rec = f.get("reconciliation") or []
        d.add_text(s, 9.1, 1.45, 3.7, 0.25, "Where two sources publish the same concept", size=9, bold=True, color=self.C["muted"])
        body = []
        for r in rec:
            body.append([{"text": f"{r['concept']}: ", "bold": True, "size": 8.5},
                         {"text": f"{(r['primary'] or {}).get('series_id')} {num((r['primary'] or {}).get('value'), 1, '%')} "
                                  f"({(r['primary'] or {}).get('period')}) vs {(r['secondary'] or {}).get('series_id')} "
                                  f"{num((r['secondary'] or {}).get('value'), 1, '%')} ({(r['secondary'] or {}).get('period')}). "
                                  f"{r['note'][:1].upper()}{r['note'][1:]} — {r['resolution']}.", "size": 8}])
        d.add_text(s, 9.1, 1.72, 3.7, 4.6, body or [[{"text": "No overlapping concepts in this edition.", "size": 9}]],
                   size=8.5, color=self.C["text"], space_after=4, autofit=True)
        d.add_footer(s, self._source(["CBA monetary policy decisions, Monetary Policy Review, Financial Stability Report"]), d.page)
        d.add_notes(s, "Decision rates come from the decision table printed in the Monetary Policy Review. Projections are "
                       "the Central Bank's own and are tagged with the round they were published in. Stress results carry "
                       "their scenario and the exercise they start from and are never presented as forecasts.")
        self.slides_index.append({"id": "A05", "page": d.page, "title": "Policy decisions, projections and stress results"})

    def a06(self):
        f = self.fp["slides"]["A06"]
        d = self.d
        s = d.new_slide()
        d.add_title(s, "A06 · Publication register and extraction provenance", "Appendix",
                    "Every narrative publication behind this edition, with its languages, dates and file hashes")
        rows = []
        for p in (f.get("publications") or [])[:16]:
            docs = p.get("documents") or []
            rows.append([p.get("type", "")[:26], p.get("edition") or "", p.get("reporting_period_end") or "not stated",
                         p.get("published_at") or "unknown", ", ".join(p.get("languages") or []),
                         (docs[0].get("sha256") or "")[:12] if docs else ""])
        d.add_table(s, 0.45, 1.45, 12.35, 4.0, ["Publication", "Edition", "Reporting period end", "Published", "Languages", "sha256 (first file)"],
                    rows, col_widths=[2.6, 2.0, 2.2, 1.9, 1.6, 2.05], font_size=8)
        notes = [[{"text": "Archive gaps: ", "bold": True, "size": 9},
                  {"text": "; ".join(f"{g['pub_type']} — {g['gap']} (expected {g['expected']})" for g in (f.get("archive_gaps") or []))
                           or "none recorded", "size": 9}],
                 [{"text": "Information cutoff: ", "bold": True, "size": 9}, {"text": f.get("cutoff_note", ""), "size": 9}]]
        lim = f.get("cutoff_limitations")
        if lim:
            notes.append([{"text": "Limitation: ", "bold": True, "size": 9}, {"text": lim.get("note", ""), "size": 9}])
        held = f.get("held_for_review") or []
        notes.append([{"text": "Held for review: ", "bold": True, "size": 9},
                      {"text": (f"{len(held)} extracted reading(s) are inconsistent with the other editions of their "
                                f"series and are excluded from every figure in this deck ("
                                + "; ".join(f"{h['series_id'].split('.')[-1]} {h['period_end']} = {h['value']}{h['unit'] or ''}"
                                            for h in held[:5]) + ")") if held else
                               "no extracted reading is currently held for review", "size": 9}])
        d.add_text(s, 0.45, 5.55, 12.35, 0.95, notes, size=9, color=self.C["text"], space_after=3, autofit=True)
        d.add_footer(s, self._source(["CBA publication pages for the policy review, decisions, policy statement and stability report"]), d.page)
        d.add_notes(s, "Both language editions of a publication are stored and are citable; numbers are extracted from one "
                       "designated edition per publication type so a translation cannot look like a revision. Every passage "
                       "keeps its page index and the page number printed on the page.")
        self.slides_index.append({"id": "A06", "page": d.page, "title": "Publication register and provenance"})
