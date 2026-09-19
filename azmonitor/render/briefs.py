"""Briefs published when a policy review, a stability report or a rate decision appears.

Each slide answers the same four questions in the same order: what changed, what the evidence is,
why it matters for a bank, and what to watch next. Where the publication gives no evidence for a
section, the section is dropped rather than padded, so a thin release produces a short brief.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .. import config
from ..narrative.contract import flatten, slide_text
from ..narrative.fmt import num, plabel
from .builder import Deck
from .policy_slides import _d, _pct


def _snap(item: dict[str, Any] | None) -> tuple[str, str]:
    if not item:
        return "n/a", ""
    snap = item.get("snapshot") or {}
    latest = snap.get("latest") or {}
    unit = snap.get("unit") or ""
    return (num(latest.get("value"), 1, "%" if unit.startswith("%") else unit),
            plabel(latest.get("period"), snap.get("period_type")))


class BriefRenderer:
    """Shared frame for the three briefs."""

    def __init__(self, pack: dict[str, Any], nar: dict[str, Any], theme: dict[str, Any], lang: str = "en"):
        self.pack = pack
        self.nar = flatten(nar or {})
        self.d = Deck(theme, lang)
        self.C = theme["colors"]
        self.SC = theme["series_colors"]
        self.L = theme["layout"]
        self.lang = lang
        self.settings = config.settings()
        self.slides_index: list[dict[str, Any]] = []
        self.pub = pack.get("publication") or {}
        self.prev = pack.get("previous_publication") or {}

    # ------------------------------------------------------------------ helpers
    def _title(self, sid: str, fallback: str) -> str:
        return slide_text(self.nar, sid)["title"] or fallback

    def _text(self, sid: str) -> dict[str, Any]:
        return slide_text(self.nar, sid)

    def _source_line(self) -> str:
        docs = self.pub.get("documents") or []
        langs = ", ".join(self.pub.get("languages") or [])
        return (f"Source: CBA {self.pub.get('type')} {self.pub.get('edition')} "
                f"(reporting period {self.pub.get('reporting_period_end') or 'not stated'}, published "
                f"{self.pub.get('published_at') or 'date unknown'}; editions: {langs}). "
                f"Retrieved {str((docs[0] or {}).get('retrieved_at', ''))[:10]}; information cutoff {self.pack['as_of']} (Asia/Baku).")

    def _passage_notes(self, topics: tuple[str, ...], limit: int = 4) -> str:
        rows = [p for p in self.pack.get("passages") or [] if any(t in p["topics"] for t in topics)][:limit]
        return "\n".join(f"- {p['cite']}: {p['text'][:300]}" for p in rows)

    def cover(self, sid: str, headline: str, subtitle: str, bullets: list[str]):
        d, C = self.d, self.C
        s = d.new_slide()
        d.add_rect(s, 0, 0, 4.9, 7.5, C["primary"], None, radius=None)
        d.add_rect(s, 4.9, 0, 0.08, 7.5, C["gold"], None, radius=None)
        d.add_text(s, 0.55, 0.95, 3.9, 0.3, self.pack["title"].upper(), size=10, bold=True, color="DDD0EA")
        d.add_text(s, 0.55, 1.3, 3.9, 1.5, self._title(sid, headline), size=24, bold=True, color=C["white"], autofit=True)
        d.add_text(s, 0.55, 2.9, 3.9, 1.1, subtitle, size=11, color=C["white"], autofit=True)
        rep = self.settings.get("report", {})
        d.add_text(s, 0.55, 5.6, 3.9, 1.0,
                   f"{rep.get('status_label', '')}. Information cutoff {self.pack['as_of']} 23:59 Asia/Baku.\n"
                   f"Generated {self.pack['generated_at'][:16].replace('T', ' ')} UTC.", size=9, color=C["white"])
        d.add_text(s, 0.55, 6.8, 3.9, 0.3,
                   f"{rep.get('organisation_label', '')}  |  {rep.get('audience_label', '')}",
                   size=9, color=C["white"])
        d.add_panel(s, 5.5, 1.3, 7.3, 4.2)
        d.add_text(s, 5.8, 1.55, 6.7, 3.7, [[{"text": b, "size": 10.5}] for b in bullets if b], size=10.5,
                   color=C["text"], bullets=True, space_after=7, autofit=True)
        d.add_text(s, 5.5, 5.7, 7.3, 1.2, self._source_line(), size=8, color=C["muted"], autofit=True)
        self.slides_index.append({"id": sid, "page": d.page, "title": self._title(sid, headline)})

    def content(self, sid: str, title: str, section: str, desc: str, draw, kpis: list[tuple[str, str, str]],
                so_what: str, notes: str):
        d = self.d
        s = d.new_slide()
        d.add_title(s, self._title(sid, title), section, desc)
        L = self.L
        x0, y0 = L["margin_x"], L["content_top"]
        main_w, right_x, right_w = 7.75, 8.45, 4.43
        draw(s, x0, y0, main_w, 5.25)
        tw, th, gap = (right_w - 0.1) / 2, 0.92, 0.08
        for i, (val, lbl, sub) in enumerate(kpis[:4]):
            col, row = i % 2, i // 2
            d.add_kpi(s, right_x + col * (tw + gap), y0 + row * (th + gap), tw, th, val, lbl, sub,
                      value_size=(16 if len(val) > 11 else None))
        rows_used = (min(len(kpis), 4) + 1) // 2
        y = y0 + rows_used * (th + gap) + 0.05
        so_h, so_y = 1.45, L["content_bottom"] - 1.45
        tx = self._text(sid)
        interp = tx["interpretations"] or []
        if interp:
            d.add_text(s, right_x, y, right_w, max(0.4, so_y - y - 0.08),
                       [[{"text": t, "size": 9.5}] for t in interp[:3]], size=9.5, color=self.C["text"],
                       bullets=True, space_after=3, autofit=True)
        d.add_so_what(s, right_x, so_y, right_w, so_h, tx["so_what"] or so_what, heading="WHY IT MATTERS")
        d.add_footer(s, self._source_line(), d.page)
        d.add_notes(s, notes)
        self.slides_index.append({"id": sid, "page": d.page, "title": self._title(sid, title)})
        return s

    def implications(self, sid: str, rows: list[dict[str, Any]], watch: list[list[str]]):
        d, C = self.d, self.C
        s = d.new_slide()
        d.add_title(s, self._title(sid, "What this means for the bank and what to monitor"), "Management implications",
                    "What changed → evidence → why it matters → what to monitor")
        cw = (12.35 - 0.15 * (len(rows) - 1)) / max(1, len(rows)) if rows else 12.35
        for i, r in enumerate(rows[:3]):
            x = 0.45 + i * (cw + 0.15)
            d.add_rect(s, x, 1.45, cw, 0.95, C["primary"], None, radius=0.03)
            d.add_text(s, x + 0.15, 1.5, cw - 0.3, 0.85, r.get("question") or r.get("evidence", ""), size=9.5,
                       bold=True, color=C["white"], anchor="m", autofit=True)
            d.add_panel(s, x, 2.4, cw, 2.3)
            body = [[{"text": "Evidence: ", "bold": True, "size": 9}, {"text": r.get("evidence", ""), "size": 9}],
                    [{"text": "Channel: ", "bold": True, "size": 9}, {"text": r.get("channel", ""), "size": 9}],
                    [{"text": "Monitor: ", "bold": True, "size": 9}, {"text": r.get("monitor", ""), "size": 9}],
                    [{"text": "Function: ", "bold": True, "size": 9}, {"text": r.get("function", ""), "size": 9}],
                    [{"text": "Classification: ", "bold": True, "size": 9}, {"text": r.get("classification", ""), "size": 9}]]
            d.add_text(s, x + 0.15, 2.5, cw - 0.3, 2.15, body, size=9, color=C["text"], space_after=3, autofit=True)
        if watch:
            d.add_text(s, 0.45, 4.85, 12.35, 0.25, "What to watch next", size=9, bold=True, color=C["muted"])
            d.add_table(s, 0.45, 5.12, 12.35, 1.6, ["Item", "Source", "Expected", "Status"], watch,
                        col_widths=[5.6, 2.4, 2.0, 2.35], font_size=8.5)
        d.add_footer(s, self._source_line(), d.page)
        d.add_notes(s, "Implications are conditional readings of public evidence. Nothing here asserts an exposure, a "
                       "vulnerability or a breach at this bank: none of these sources contains bank-level data.")
        self.slides_index.append({"id": sid, "page": d.page, "title": "Management implications"})

    def provenance(self, sid: str):
        d = self.d
        s = d.new_slide()
        d.add_title(s, "Sources and extraction record", "Appendix",
                    "The files this brief was built from, and what could not be extracted from them")
        notes = self.pack.get("extraction_notes") or {}
        rows = [[doc.get("language") or "", str(doc.get("pages") or ""), str(doc.get("version") or ""),
                 doc.get("sha256") or "", (doc.get("url") or "")[:70]] for doc in notes.get("documents") or []]
        d.add_table(s, 0.45, 1.45, 12.35, 1.8, ["Language", "Pages", "Version", "sha256", "URL"], rows,
                    col_widths=[1.1, 0.8, 0.9, 2.2, 7.35], font_size=8)
        passages = self.pack.get("passages") or []
        prows = [[p["cite"], (p.get("section") or "")[:28], p["text"][:150]] for p in passages[:8]]
        d.add_text(s, 0.45, 3.45, 12.35, 0.25, "Quotable passages behind this brief", size=9, bold=True, color=self.C["muted"])
        d.add_table(s, 0.45, 3.72, 12.35, 2.6, ["Page", "Section", "Passage"], prows,
                    col_widths=[1.2, 2.2, 8.95], font_size=7.5)
        d.add_text(s, 0.45, 6.4, 12.35, 0.5,
                   f"{notes.get('passages', 0)} passages stored, {notes.get('passages_needing_review', 0)} flagged for "
                   f"review. {notes.get('note', '')}", size=8.5, color=self.C["muted"], autofit=True)
        d.add_footer(s, self._source_line(), d.page)
        self.slides_index.append({"id": sid, "page": d.page, "title": "Sources and extraction record"})

    def save(self, out_path: Path) -> dict[str, Any]:
        self.d.save(out_path)
        return {"path": str(out_path), "slides": self.slides_index, "n_slides": self.d.page}


class MprBrief(BriefRenderer):
    def render(self, out_path: Path) -> dict[str, Any]:
        pol = self.pack["policy"]
        decision = pol.get("decision") or {}
        fc = pol.get("forecasts") or {}
        self.cover("P01", f"{self.pub.get('type')} {self.pub.get('edition')}: what changed",
                   f"Reporting period {self.pub.get('reporting_period_end') or 'not stated in the publication'} · "
                   f"published {_d(self.pub.get('published_at'))} · compared with {self.prev.get('edition') or 'no earlier edition'}",
                   [pol["stance"].get("statement") or "",
                    f"Refinancing rate {_pct(decision.get('policy_rate'))}, corridor "
                    f"{_pct(decision.get('corridor_floor'))} to {_pct(decision.get('corridor_ceiling'))}.",
                    f"Projections published in the {fc.get('current_vintage')} round; "
                    f"{len([r for r in fc.get('revisions') or [] if r.get('comparable')])} target period(s) are comparable "
                    f"with the {fc.get('previous_vintage')} round.",
                    f"Next decision: {_d((pol.get('next_decision') or {}).get('date'))} "
                    f"({(pol.get('next_decision') or {}).get('status')}).",
                    "Numbers in this brief are the Central Bank's own; nothing is our projection."])
        self.p02_decision(pol)
        self.p03_inflation(pol)
        self.p04_transmission()
        self.implications("P05", self._implications(), self._watch())
        self.provenance("P06")
        return self.save(out_path)

    def p02_decision(self, pol: dict[str, Any]):
        d = self.d
        decision = pol.get("decision") or {}
        kpis = [(_pct(decision.get("policy_rate")), "Refinancing rate", _d(decision.get("announcement_date"))),
                (f"{_pct(decision.get('corridor_floor'))} – {_pct(decision.get('corridor_ceiling'))}", "Corridor", ""),
                (f"{pol['stance'].get('rate_change_bp'):+.0f} bp" if isinstance(pol["stance"].get("rate_change_bp"), (int, float)) else "n/a",
                 "Change at this meeting", pol["stance"].get("label") or ""),
                (_d((pol.get("next_decision") or {}).get("date")), "Next decision",
                 (pol.get("next_decision") or {}).get("status") or "")]

        def draw(s, x, y, w, h):
            path = pol.get("rate_path") or []
            floor = {p["date"]: p["value"] for p in (pol.get("corridor_path") or {}).get("floor", [])}
            ceil = {p["date"]: p["value"] for p in (pol.get("corridor_path") or {}).get("ceiling", [])}
            cats = [_d(p["date"]) for p in path]
            d.add_line_chart(s, x, y + 0.25, w, h * 0.5, cats,
                             [{"name": "Ceiling", "values": [ceil.get(p["date"]) for p in path], "color": self.SC.get("fx", "#B39DDB")},
                              {"name": "Refinancing rate", "values": [p["value"] for p in path], "color": self.SC.get("total", "#6F00B6")},
                              {"name": "Floor", "values": [floor.get(p["date"]) for p in path], "color": self.SC.get("deposits", "#00897B")}],
                             number_format="0.00", skip=max(1, len(cats) // 8))
            d.add_text(s, x, y + h * 0.5 + 0.35, w, 0.25, "The Central Bank's stated reasoning", size=9, bold=True, color=self.C["muted"])
            d.add_text(s, x, y + h * 0.5 + 0.6, w, h * 0.5 - 0.6,
                       [[{"text": (decision.get("rationale") or "")[:900], "size": 9}]], size=9,
                       color=self.C["text"], autofit=True)

        self.content("P02", "The decision and the reasoning behind it", "Decision",
                     f"Decision of {_d(decision.get('announcement_date'))}; effective date "
                     f"{decision.get('effective_date') or 'not stated'}", draw, kpis,
                     pol["stance"].get("statement") or "",
                     f"Effective date basis: {decision.get('effective_date_basis')}. "
                     f"Rate levels come from the decision table in the review; the rationale from the decision statement.\n\n"
                     + self._passage_notes(("risk", "inflation", "inflyasiya")))

    def p03_inflation(self, pol: dict[str, Any]):
        d = self.d
        fc = pol.get("forecasts") or {}
        current = fc.get("current") or []
        infl = [x for x in current if x["series_id"] == "cba.forecast.inflation"]
        kpis = [(_pct(x["value"], 1), f"Inflation projection, {x.get('horizon')}", f"{fc.get('current_vintage')} round")
                for x in infl[:3]]
        growth = [x for x in current if "gdp" in x["series_id"]]
        if growth:
            kpis.append((_pct(growth[0]["value"], 1), f"Growth projection, {growth[0].get('horizon')}",
                         f"{fc.get('current_vintage')} round"))

        def draw(s, x, y, w, h):
            rows = [[x_.get("label") or x_["series_id"], x_.get("horizon") or "", x_["observation_date"],
                     _pct(x_["value"], 1), x_.get("scenario") or "baseline"] for x_ in current]
            d.add_text(s, x, y, w, 0.25, f"Projections published in the {fc.get('current_vintage')} round", size=9,
                       bold=True, color=self.C["muted"])
            d.add_table(s, x, y + 0.25, w, h * 0.42, ["Projection", "Horizon", "Target period", "Value", "Scenario"],
                        rows, col_widths=[2.6, 1.5, 1.5, 1.0, 1.15], font_size=8)
            comparable = [r for r in fc.get("revisions") or [] if r.get("comparable")]
            d.add_text(s, x, y + h * 0.42 + 0.35, w, 0.25,
                       f"Revisions against the {fc.get('previous_vintage')} round (same target period only)",
                       size=9, bold=True, color=self.C["muted"])
            if comparable:
                rrows = [[r["series_id"].split(".")[-1], r["target_period"], _pct(r["previous"], 1), _pct(r["current"], 1),
                          f"{r['revision_pp']:+.1f} pp"] for r in comparable]
                d.add_table(s, x, y + h * 0.42 + 0.6, w, h * 0.35,
                            ["Projection", "Target", fc.get("previous_vintage") or "previous",
                             fc.get("current_vintage") or "current", "Revision"], rrows,
                            col_widths=[2.6, 1.5, 1.5, 1.5, 1.15], font_size=8)
            else:
                d.add_text(s, x, y + h * 0.42 + 0.62, w, 0.8,
                           "No target period appears in both rounds with an extractable value, so no like-for-like "
                           "revision is shown. The table above states which round each projection comes from.",
                           size=9, color=self.C["muted"], autofit=True)

        self.content("P03", "Inflation and growth as the Central Bank projects them", "Projections",
                     f"{fc.get('current_vintage')} round; comparison with {fc.get('previous_vintage')}", draw, kpis,
                     "These are the Central Bank's published projections. They are not outcomes and not our own scenarios.",
                     "Every projection carries the round it was published in, the target period, the definition and the "
                     "scenario. Revisions are only computed where the same target period appears in both rounds.\n\n"
                     + self._passage_notes(("proqnoz", "forecast", "inflyasiya", "inflation")))

    def p04_transmission(self):
        d = self.d
        ctx = {i["label"]: i for i in (self.pack.get("context") or {}).get("items", [])}
        loan = ctx.get("Average rate on new AZN loans")
        dep = ctx.get("Average rate on new AZN term deposits")
        spread = ctx.get("Indicative AZN pricing spread")
        kpis = [(_snap(loan)[0], "New AZN loan rate", _snap(loan)[1]), (_snap(dep)[0], "New AZN term deposit rate", _snap(dep)[1]),
                (_snap(spread)[0], "Indicative spread", _snap(spread)[1]),
                (_snap(ctx.get("Loans to the economy, y/y"))[0], "Loans, y/y", _snap(ctx.get("Loans to the economy, y/y"))[1])]

        def draw(s, x, y, w, h):
            rows = [[i["label"], _snap(i)[0], _snap(i)[1]] for i in (self.pack.get("context") or {}).get("items", [])]
            d.add_text(s, x, y, w, 0.25, "Where the policy rate shows up in the monthly banking statistics",
                       size=9, bold=True, color=self.C["muted"])
            d.add_table(s, x, y + 0.25, w, h * 0.75, ["Measure", "Latest", "Reporting period"], rows,
                        col_widths=[4.3, 1.6, 1.85], font_size=8.5)
            d.add_text(s, x, y + h * 0.75 + 0.35, w, h * 0.25 - 0.35,
                       "Transmission is read from published series only: money-market and bank rates, the deposit and "
                       "credit aggregates. No pass-through coefficient is estimated here.",
                       size=9, color=self.C["muted"], autofit=True)

        self.content("P04", "Transmission: what the monthly data show", "Transmission",
                     "New-business rates, credit and deposit growth around the decision", draw, kpis,
                     "Pricing and volumes are the observable channel between the corridor and a bank's margin.",
                     "Series come from the monthly CBA tables, not from the review, so they carry their own reporting "
                     "periods. The review's own commentary on the credit and deposit market is quoted in the notes.\n\n"
                     + self._passage_notes(("kredit", "credit", "depozit", "deposit", "likvidlik", "liquidity")))

    def _implications(self) -> list[dict[str, Any]]:
        pol = self.pack["policy"]
        decision = pol.get("decision") or {}
        fc = pol.get("forecasts") or {}
        infl = next((x for x in fc.get("current") or [] if x["series_id"] == "cba.forecast.inflation"), None)
        rows = [{
            "question": "How does the corridor decision sit against our deposit repricing schedule?",
            "evidence": pol["stance"].get("statement") or "",
            "channel": "The corridor sets the floor under manat funding costs and the return on placements with the Central Bank",
            "monitor": "New-business deposit and loan rates (CBA table 3.2.1), interbank rates, our own cost of funds",
            "function": "Treasury / ALM", "classification": "interpretation",
        }]
        if infl:
            rows.append({
                "question": "Do our planning assumptions differ from the published projection?",
                "evidence": f"The {fc.get('current_vintage')} round projects inflation of {infl['value']}% for {infl.get('horizon')}",
                "channel": "Inflation drives nominal income growth, deposit demand and the real cost of borrowing",
                "monitor": "Monthly CPI, the next projection round, deposit growth by segment",
                "function": "Finance / Strategy", "classification": "cba_forecast",
            })
        rows.append({
            "question": "What would make the Central Bank move before the next scheduled decision?",
            "evidence": "The statement names the conditions it is watching",
            "channel": "An unscheduled change would reprice the whole manat curve",
            "monitor": f"Decision statement of {_d((pol.get('next_decision') or {}).get('date'))}; FX market and liquidity data",
            "function": "Treasury / Risk", "classification": "management_question",
        })
        return rows

    def _watch(self) -> list[list[str]]:
        pol = self.pack["policy"]
        nd = pol.get("next_decision") or {}
        return [["Next interest rate corridor decision", "CBA press release", _d(nd.get("date")), nd.get("status") or ""],
                ["Next Monetary Policy Review", "CBA publication page", "not announced",
                 "the Central Bank publishes no calendar for this report"],
                ["Monthly interest rate statistics (table 3.2.1)", "CBA monetary statistics", "within 30 days of month end",
                 "expected from the official schedule, not confirmed"]]


class FsrBrief(BriefRenderer):
    def _this_exercise(self, stab: dict[str, Any]) -> list[dict[str, Any]]:
        """Stress results from this report's own exercise only.

        Results from two exercises are not one path: the 2023 round projected 2024 from end-2023
        data and the 2025 round projects 2026 from end-2025 data. Drawing them on one axis would
        show a trajectory nobody published.
        """
        results = stab.get("stress_tests", {}).get("results") or []
        pub_id = self.pub.get("publication_id") or self.pub.get("id")
        mine = [r for r in results if (r.get("publication") or {}).get("id") == pub_id]
        if mine:
            return mine
        exercises = sorted({(r.get("dims") or {}).get("exercise") for r in results if (r.get("dims") or {}).get("exercise")})
        return [r for r in results if not exercises or (r.get("dims") or {}).get("exercise") == exercises[-1]]

    def render(self, out_path: Path) -> dict[str, Any]:
        stab = self.pack["stability"]
        dash = {r["series_id"]: r for r in stab.get("dashboard") or []}
        car, lcr = dash.get("cba.fsr.car"), dash.get("cba.fsr.lcr")
        stress = self._this_exercise(stab)
        adverse = [x for x in stress if x.get("scenario") == "adverse"]
        self.cover("S01", f"{self.pub.get('type')} {self.pub.get('edition')}: what the Central Bank found",
                   f"Reporting period {self.pub.get('reporting_period_end')} · published {_d(self.pub.get('published_at'))} · "
                   f"compared with {self.prev.get('edition') or 'no earlier edition'}",
                   [f"Regulatory capital adequacy {_pct(car['value'], 1) if car else 'not published'} at "
                    f"{car['observation_date'] if car else 'n/a'}.",
                    f"Liquidity coverage ratio {_pct(lcr['value'], 1) if lcr else 'not published'} at "
                    f"{lcr['observation_date'] if lcr else 'n/a'}.",
                    (f"Under the published adverse scenario, capital adequacy is projected at "
                     f"{_pct(sorted(adverse, key=lambda a: a['observation_date'])[-1]['value'], 1)} by "
                     f"{sorted(adverse, key=lambda a: a['observation_date'])[-1]['observation_date'][:4]} — a stress "
                     f"projection, not a forecast." if adverse else
                     "No stress-test result could be extracted from this edition."),
                    "These measures are half-yearly and are older than the monthly banking data in the monitor; both "
                    "dates are shown wherever they appear."])
        self.s02_capital(stab)
        self.s03_credit(stab)
        if stress:
            self.s04_stress(stab)
        self.implications("S05", self._implications(stab), self._watch())
        self.provenance("S06")
        return self.save(out_path)

    def s02_capital(self, stab: dict[str, Any]):
        d = self.d
        dash = stab.get("dashboard") or []
        by = {r["series_id"]: r for r in dash}
        kpis = []
        for sid, label in (("cba.fsr.car", "Capital adequacy (regulatory)"), ("cba.fsr.lcr", "Liquidity coverage ratio"),
                           ("cba.fsr.roe", "Return on equity"), ("cba.fsr.roa", "Return on assets")):
            r = by.get(sid)
            kpis.append((_pct(r["value"], 1) if r else "not published", label,
                         f"{r['observation_date']} · published {_d(r['publication']['published_at'])}" if r else ""))

        def draw(s, x, y, w, h):
            rows = [[(r.get("label") or r["series_id"])[:46], _pct(r["value"], 1), r["observation_date"],
                     _d(r["publication"]["published_at"]),
                     (f"{r['change']:+.1f} pp" if isinstance(r.get("change"), (int, float)) else "not consecutive")]
                    for r in dash]
            d.add_text(s, x, y, w, 0.25, "Indicators as published, each with its own reporting date", size=9, bold=True,
                       color=self.C["muted"])
            d.add_table(s, x, y + 0.25, w, h * 0.55, ["Indicator", "Value", "Reporting date", "Published", "Change"],
                        rows, col_widths=[3.0, 0.9, 1.3, 1.2, 1.35], font_size=8)
            recs = stab.get("source_reconciliation") or []
            d.add_text(s, x, y + h * 0.55 + 0.35, w, h * 0.45 - 0.35,
                       [[{"text": "Regulatory versus book measures: ", "bold": True, "size": 9},
                         {"text": "regulatory capital adequacy and the liquidity coverage ratio come from this report; "
                                  "capital-to-assets and liquid-assets-to-assets come from the monthly balance sheet. "
                                  "They are different concepts and are not comparable.", "size": 9}]] +
                       [[{"text": f"{r['concept']}: ", "bold": True, "size": 9}, {"text": r["note"], "size": 8.5}]
                        for r in recs],
                       size=9, color=self.C["text"], space_after=4, autofit=True)

        self.content("S02", "Capital, liquidity and profitability", "Capital and liquidity",
                     f"Reporting period {self.pub.get('reporting_period_end')}, published {_d(self.pub.get('published_at'))}",
                     draw, kpis,
                     "Regulatory capital and liquidity are only published here, twice a year; the monthly tables carry "
                     "book measures that answer a different question.",
                     self._passage_notes(("capital adequacy", "liquidity", "profitability")))

    def s03_credit(self, stab: dict[str, Any]):
        d = self.d
        by = {r["series_id"]: r for r in stab.get("dashboard") or []}
        ctx = {i["label"]: i for i in (self.pack.get("context") or {}).get("items", [])}
        npl = by.get("cba.fsr.npl_ratio")
        kpis = [(_pct(npl["value"], 1) if npl else "not published", "NPL ratio (stability report)",
                 npl["observation_date"] if npl else ""),
                (_snap(ctx.get("NPL ratio (monthly prudential table)"))[0], "NPL ratio (monthly table)",
                 _snap(ctx.get("NPL ratio (monthly prudential table)"))[1]),
                (_snap(ctx.get("Loans to the economy, y/y"))[0], "Loans, y/y", _snap(ctx.get("Loans to the economy, y/y"))[1]),
                (_snap(ctx.get("FX share of deposits"))[0], "FX share of deposits", _snap(ctx.get("FX share of deposits"))[1])]

        def draw(s, x, y, w, h):
            d.add_text(s, x, y, w, 0.25, "Credit quality and household exposure as the report describes them",
                       size=9, bold=True, color=self.C["muted"])
            rows = [[p["cite"], p["text"][:220]] for p in (self.pack.get("passages") or [])
                    if any(t in p["topics"] for t in ("npl", "non-performing", "household", "mortgage", "concentration"))][:6]
            d.add_table(s, x, y + 0.25, w, h * 0.62, ["Page", "What the report says"], rows,
                        col_widths=[1.0, 6.75], font_size=7.5)
            d.add_text(s, x, y + h * 0.62 + 0.3, w, h * 0.38 - 0.3,
                       [[{"text": "Definitions differ: ", "bold": True, "size": 9},
                         {"text": "the monthly prudential table and this report both publish a non-performing loan ratio "
                                  "with different coverage and timing. Both are shown with their own reporting dates and "
                                  "neither replaces the other.", "size": 9}]],
                       size=9, color=self.C["text"], autofit=True)

        self.content("S03", "Credit quality, households and concentrations", "Credit quality",
                     "As described in the report, beside the monthly statistics", draw, kpis,
                     "Where the two sources disagree, the difference is one of definition, not a discrepancy to resolve.",
                     self._passage_notes(("npl", "non-performing", "household", "mortgage", "concentration")))

    def s04_stress(self, stab: dict[str, Any]):
        d = self.d
        results = self._this_exercise(stab)
        scenarios = sorted({r.get("scenario") for r in results if r.get("scenario")})
        years = sorted({r["observation_date"][:4] for r in results})
        kpis = []
        for sc in scenarios[:2]:
            last = sorted([r for r in results if r.get("scenario") == sc], key=lambda r: r["observation_date"])[-1]
            kpis.append((_pct(last["value"], 1), f"Capital adequacy, {sc} scenario", f"projection for {last['observation_date'][:4]}"))

        def draw(s, x, y, w, h):
            colours = {"baseline": self.SC.get("total", "#6F00B6"), "adverse": self.SC.get("overdue", "#E53935")}
            series = []
            for sc in scenarios:
                series.append({"name": f"{sc} scenario",
                               "values": [next((r["value"] for r in results if r.get("scenario") == sc
                                                and r["observation_date"][:4] == yr), None) for yr in years],
                               "color": colours.get(sc, "#9E9E9E")})
            d.add_text(s, x, y, w, 0.25, "Projected regulatory capital adequacy under the published scenarios (%)",
                       size=9, bold=True, color=self.C["muted"])
            d.add_bar_chart(s, x, y + 0.25, w * 0.5 - 0.1, h * 0.55, years, series, number_format="0.0",
                            data_labels=True, gap_width=60)
            # a python-pptx table grows to fit its text, so the rows are kept short enough that the
            # table stays inside its box and does not run over the note beneath it
            rows = [[p["cite"], p["text"][:130] + ("…" if len(p["text"]) > 130 else "")]
                    for p in (self.pack.get("passages") or []) if "stress" in p["topics"]][:4]
            d.add_table(s, x + w * 0.5, y + 0.25, w * 0.5, h * 0.55, ["Page", "Scenario description"], rows,
                        col_widths=[0.9, 2.98], font_size=7)
            d.add_text(s, x, y + h * 0.55 + 0.35, w, h * 0.45 - 0.35,
                       [[{"text": stab.get("stress_tests", {}).get("note", ""), "size": 9}]],
                       size=9, color=self.C["text"], autofit=True)

        self.content("S04", "Stress-test scenarios and what they project", "Stress tests",
                     f"Top-down exercise starting from {(results[0].get('dims') or {}).get('exercise', 'n/a')} data",
                     draw, kpis,
                     "An adverse-scenario result is a projection under an assumed shock, not a forecast and not a loss "
                     "that has occurred.",
                     self._passage_notes(("stress",)))

    def _implications(self, stab: dict[str, Any]) -> list[dict[str, Any]]:
        by = {r["series_id"]: r for r in stab.get("dashboard") or []}
        car = by.get("cba.fsr.car")
        lcr = by.get("cba.fsr.lcr")
        stress = [r for r in stab.get("stress_tests", {}).get("results") or [] if r.get("scenario") == "adverse"]
        rows = []
        if car:
            rows.append({
                "question": "How does our capital headroom compare with the sector?",
                "evidence": f"Sector capital adequacy {_pct(car['value'], 1)} at {car['observation_date']}, published "
                            f"{_d(car['publication']['published_at'])}",
                "channel": "System headroom conditions supervisory tolerance and how peers price risk",
                "monitor": "Our own regulatory ratio against the requirement; the next stability report",
                "function": "CRO / Capital management", "classification": "cba_assessment"})
        if stress:
            last = sorted(stress, key=lambda r: r["observation_date"])[-1]
            rows.append({
                "question": "What would the published adverse scenario do to our own capital?",
                "evidence": f"Sector capital adequacy projected at {_pct(last['value'], 1)} for "
                            f"{last['observation_date'][:4]} under the adverse scenario",
                "channel": "The scenario's shocks map onto our credit, market and FX exposures",
                "monitor": "Our internal stress results on the same scenario; concentration limits",
                "function": "Risk / ALCO", "classification": "cba_assessment"})
        if lcr:
            rows.append({
                "question": "Is our liquidity buffer positioned like the sector's?",
                "evidence": f"Sector liquidity coverage ratio {_pct(lcr['value'], 1)} at {lcr['observation_date']}",
                "channel": "Buffer size drives the cost of holding liquidity and the tolerance for deposit outflow",
                "monitor": "Our own coverage ratio by currency; deposit concentration and maturity profile",
                "function": "Treasury / ALM", "classification": "interpretation"})
        return rows

    def _watch(self) -> list[list[str]]:
        return [["Next Financial Stability Report", "CBA publication page", "not announced",
                 "half-yearly in practice; the Central Bank publishes no calendar for it"],
                ["Monthly prudential tables 5.2 to 5.6", "CBA banking statistics", "within 30 days of month end",
                 "expected from the official schedule, not confirmed"]]


class DecisionUpdate(BriefRenderer):
    def render(self, out_path: Path) -> dict[str, Any]:
        pol = self.pack["policy"]
        decision = pol.get("decision") or {}
        self.cover("D01", f"Policy decision, {_d(decision.get('announcement_date'))}",
                   f"{pol['stance'].get('label')} · announced {_d(decision.get('announcement_date'))} · "
                   f"effective date {decision.get('effective_date') or 'not stated'}",
                   [pol["stance"].get("statement") or "",
                    f"Corridor: {_pct(decision.get('corridor_floor'))} floor, {_pct(decision.get('policy_rate'))} "
                    f"refinancing rate, {_pct(decision.get('corridor_ceiling'))} ceiling.",
                    (decision.get("rationale") or "")[:420],
                    f"Next decision: {_d((pol.get('next_decision') or {}).get('date'))} "
                    f"({(pol.get('next_decision') or {}).get('status')})."])
        self.d02(pol)
        self.implications("D03", self._implications(pol), self._watch(pol))
        return self.save(out_path)

    def d02(self, pol: dict[str, Any]):
        d = self.d
        decision = pol.get("decision") or {}
        previous = pol.get("previous_decision") or {}
        ctx = {i["label"]: i for i in (self.pack.get("context") or {}).get("items", [])}
        kpis = [(_pct(decision.get("policy_rate")), "Refinancing rate", _d(decision.get("announcement_date"))),
                (f"{pol['stance'].get('rate_change_bp'):+.0f} bp" if isinstance(pol["stance"].get("rate_change_bp"), (int, float)) else "n/a",
                 "Change", pol["stance"].get("label") or ""),
                (_snap(ctx.get("CPI inflation, y/y"))[0], "CPI inflation", _snap(ctx.get("CPI inflation, y/y"))[1]),
                (_snap(ctx.get("Average rate on new AZN loans"))[0], "New AZN loan rate",
                 _snap(ctx.get("Average rate on new AZN loans"))[1])]

        def draw(s, x, y, w, h):
            rows = [["Refinancing rate", _pct(previous.get("policy_rate")), _pct(decision.get("policy_rate"))],
                    ["Corridor floor", _pct(previous.get("corridor_floor")), _pct(decision.get("corridor_floor"))],
                    ["Corridor ceiling", _pct(previous.get("corridor_ceiling")), _pct(decision.get("corridor_ceiling"))],
                    ["Announced", _d(previous.get("announcement_date")), _d(decision.get("announcement_date"))],
                    ["Effective date", previous.get("effective_date") or "not stated", decision.get("effective_date") or "not stated"]]
            d.add_text(s, x, y, w * 0.45, 0.25, "This decision against the previous one", size=9, bold=True, color=self.C["muted"])
            d.add_table(s, x, y + 0.25, w * 0.45, h * 0.5, ["", "Previous", "Current"], rows,
                        col_widths=[1.4, 1.0, 1.05], font_size=8)
            d.add_text(s, x + w * 0.47, y, w * 0.53, 0.25, "What the statement says", size=9, bold=True, color=self.C["muted"])
            d.add_text(s, x + w * 0.47, y + 0.25, w * 0.53, h * 0.75,
                       [[{"text": (decision.get("rationale") or "")[:1100], "size": 9}]], size=9,
                       color=self.C["text"], autofit=True)
            rows2 = [[i["label"], _snap(i)[0], _snap(i)[1]] for i in (self.pack.get("context") or {}).get("items", [])[:6]]
            d.add_text(s, x, y + h * 0.56, w * 0.45, 0.25, "Banking context at the latest month", size=9, bold=True,
                       color=self.C["muted"])
            d.add_table(s, x, y + h * 0.56 + 0.25, w * 0.45, h * 0.42, ["Measure", "Latest", "Period"], rows2,
                        col_widths=[1.9, 0.75, 0.8], font_size=7.5)

        self.content("D02", "What changed, and what it bears on", "Decision detail",
                     f"Decision of {_d(decision.get('announcement_date'))} compared with {_d(previous.get('announcement_date'))}",
                     draw, kpis, pol["stance"].get("statement") or "",
                     f"Rationale language: {decision.get('rationale_language')}. Effective date basis: "
                     f"{decision.get('effective_date_basis')}.")

    def _implications(self, pol: dict[str, Any]) -> list[dict[str, Any]]:
        stance = pol.get("stance") or {}
        rows = [{
            "question": "Does this change our manat funding plan?",
            "evidence": stance.get("statement") or "",
            "channel": "The corridor floor sets the return on placements and the floor under term deposit pricing",
            "monitor": "Interbank rates, our deposit book repricing schedule, CBA table 3.2.1",
            "function": "Treasury / ALM", "classification": "interpretation"}]
        if stance.get("rationale_changed"):
            rows.append({
                "question": "The rate is unchanged but the reasoning moved — what are we reading into that?",
                "evidence": "The stated rationale differs from the previous decision while the corridor is unchanged",
                "channel": "A changed rationale signals the conditions under which the next move happens",
                "monitor": "The next decision statement and the projection round that precedes it",
                "function": "Risk / Strategy", "classification": "interpretation"})
        return rows

    def _watch(self, pol: dict[str, Any]) -> list[list[str]]:
        nd = pol.get("next_decision") or {}
        return [["Next corridor decision", "CBA press release", _d(nd.get("date")), nd.get("status") or ""],
                ["Monthly interest rate statistics", "CBA table 3.2.1", "within 30 days of month end",
                 "expected from the official schedule, not confirmed"]]


RENDERERS = {"mpr_brief": MprBrief, "fsr_brief": FsrBrief, "decision_update": DecisionUpdate}


def render_brief(kind: str, pack: dict[str, Any], nar: dict[str, Any], out_path: Path, lang: str = "en") -> dict[str, Any]:
    theme = config.theme()
    renderer = RENDERERS[kind](pack, nar, theme, lang)
    return renderer.render(out_path)
