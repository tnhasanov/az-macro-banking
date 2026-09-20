"""Monthly deck renderer: 18 main slides plus appendices, from the fact pack and narrative."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .. import config
from ..narrative.contract import flatten, slide_text
from .policy_slides import PolicyStabilitySlides
from ..narrative.fmt import money, num, plabel
from .builder import Deck, align_series, month_labels


def _v(snap: dict[str, Any] | None) -> float | None:
    return snap["latest"]["value"] if snap and snap.get("latest") else None


def _fmt(snap: dict[str, Any] | None, dec: int = 1, unit: str | None = None) -> str:
    if not snap or not snap.get("latest") or snap["latest"]["value"] is None:
        return "n/a"
    u = unit or snap.get("unit")
    v = snap["latest"]["value"]
    if u in ("AZN mln", "USD mln"):
        return money(v, u)
    if u == "AZN per month":
        return f"AZN {v:,.0f}"
    if u == "count":
        return f"{v:,.0f}"
    if u and u.startswith("%"):
        u = "%"
    return num(v, dec, u if u in ("%", "pp") else None)


def _chg(snap: dict[str, Any] | None, dec: int = 1) -> str:
    if not snap or snap.get("change") is None:
        return "no comparable prior value"
    u = snap.get("unit")
    ch = snap["change"]
    if u and (u.startswith("%") or u == "pp"):
        s = num(ch, dec, "pp", sign=True)
    elif u in ("AZN mln", "USD mln"):
        s = f"{ch:+,.0f} mln"
    else:
        s = num(ch, dec, None, sign=True)
    cmp_ = {"lag12": "y/y", "lag1": "m/m", "prior_edition": "vs prior edition"}.get(snap.get("compare"), "")
    return f"{s} {cmp_}".strip()


def _per(snap: dict[str, Any] | None) -> str:
    return plabel(snap["latest"]["period"], snap.get("period_type")) if snap and snap.get("latest") else "n/a"


def _kpi(snap: dict[str, Any] | None, label: str, dec: int = 1) -> tuple[str, str, str]:
    return (_fmt(snap, dec), label, f"{_per(snap)} · {_chg(snap, dec)}")


def _clip(text: str, n: int = 170) -> str:
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


class MonthlyRenderer(PolicyStabilitySlides):
    def __init__(self, fp: dict[str, Any], nar: dict[str, Any], theme: dict[str, Any], lang: str = "en"):
        self.fp = fp
        # a narrative may arrive grounded (blocks with claims) or already flattened; rendering only
        # ever needs the text, and flattening twice is harmless
        self.nar = flatten(nar or {})
        self.deck = Deck(theme, lang)
        self.d = self.deck
        self.lang = lang
        self.C = theme["colors"]
        self.SC = theme["series_colors"]
        self.L = theme["layout"]
        self.settings = config.settings()
        self.slides_index: list[dict[str, Any]] = []
        self.docs = {s["doc_id"]: s for s in fp.get("sources", [])}
        self.ed = fp["edition"]
        self.facts_only = nar.get("mode") == "facts_only"

    # ------------------------------------------------------------------ helpers
    def _title(self, sid: str, fallback: str) -> str:
        t = slide_text(self.nar, sid)["title"]
        return t or fallback

    def _text(self, sid: str) -> dict[str, Any]:
        return slide_text(self.nar, sid)

    def _source(self, refs: list[str]) -> str:
        parts = []
        for r in refs:
            parts.append(r)
        return "Source: " + "; ".join(parts) + f". Retrieved {self.fp['generated_at'][:10]}; information cutoff {self.fp['as_of']} (Asia/Baku)."

    def _doc_refs(self, dataset_ids: list[str]) -> str:
        lines = []
        for s in self.fp.get("sources", []):
            if s["dataset_id"] in dataset_ids:
                lines.append(f"- {s['source_id']} / {s['dataset_id']}: {s.get('title_original')} — {s.get('document_url')} (published {s.get('published_at') or 'n/a'}, retrieved {s.get('retrieved_at', '')[:10]}, sha256 {s.get('sha256', '')[:12]})")
        return "\n".join(lines)

    def _notes(self, slide, sid: str, dataset_ids: list[str], metric_ids: list[str], extra: str = ""):
        defs = {d["id"]: d for d in self.fp.get("definitions", [])}
        met = []
        for m in metric_ids:
            d = defs.get(m)
            if d:
                met.append(f"- {m}: {d.get('label') or ''} | formula {d['formula']} | inputs {d.get('inputs')} | {d.get('basis') or ''}")
            else:
                met.append(f"- {m}")
        text = f"Slide {sid}. Information cutoff {self.fp['as_of']} (Asia/Baku). Information-set mode: {self.fp.get('information_set_mode')}.\n\nSource documents:\n" + self._doc_refs(dataset_ids) + \
               "\n\nMetric definitions:\n" + "\n".join(met) + ("\n\n" + extra if extra else "")
        tx = self._text(sid)
        if tx.get("caveat"):
            text += "\n\nCaveat: " + tx["caveat"]
        self.d.add_notes(slide, text)

    def _standard(self, sid: str, fallback_title: str, section: str, desc: str, kpis: list[tuple[str, str, str]], draw_main, source_refs: list[str],
                  dataset_ids: list[str], metric_ids: list[str], so_what_default: str = "", extra_notes: str = ""):
        d = self.d
        s = d.new_slide()
        tx = self._text(sid)
        d.add_title(s, self._title(sid, fallback_title), section, desc)
        L = self.L
        x0, y0 = L["margin_x"], L["content_top"]
        main_w, right_x, right_w = 7.75, 8.45, 4.43
        draw_main(s, x0, y0, main_w, 5.25)
        # KPI tiles
        n_k = min(len(kpis), 6)
        tw, th, gap = (right_w - 0.1) / 2, (0.84 if n_k > 4 else 0.92), 0.08
        y = y0
        for i, (val, lbl, sub) in enumerate(kpis[:6]):
            col, row = i % 2, i // 2
            # a card whose label and sub-line together run past two lines is set one point smaller,
            # so the last line stays inside the panel instead of printing across its edge
            small = len(lbl) + len(sub or "") > 46
            d.add_kpi(s, right_x + col * (tw + gap), y0 + row * (th + gap), tw, th, val, lbl, sub,
                      value_size=(16 if len(val) > 11 else None), label_size=(7 if small else None))
        rows_used = (n_k + 1) // 2
        y = y0 + rows_used * (th + gap) + 0.05
        so_h = 1.25
        so_y = L["content_bottom"] - so_h
        interp = tx["interpretations"] or []
        if interp:
            paras = [[{"text": _clip(t), "size": 9.5}] for t in interp[:3]]
            d.add_text(s, right_x, y, right_w, max(0.4, so_y - y - 0.08), paras, size=9.5, color=self.C["text"], bullets=True, space_after=2, line_spacing=1.0, autofit=True)
        d.add_so_what(s, right_x, so_y, right_w, so_h, tx["so_what"] or so_what_default, heading=config.term("view", self.lang).upper() if not self.facts_only else "FACTS-ONLY NOTE")
        d.add_footer(s, self._source(source_refs), d.page)
        self._notes(s, sid, dataset_ids, metric_ids, extra_notes)
        self.slides_index.append({"id": sid, "page": d.page, "title": self._title(sid, fallback_title)})
        return s

    def _caption(self, slide, x, y, w, text):
        self.d.add_text(slide, x, y, w, 0.24, text, size=9, bold=True, color=self.C["muted"])

    def _chart_series(self, series: list[dict[str, Any]], colors: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
        periods, values = align_series(series)
        cats = month_labels(periods, self.lang)
        out = [{"name": s["label"], "values": vals, "color": colors[i % len(colors)]} for i, (s, vals) in enumerate(zip(series, values))]
        return cats, out

    # ------------------------------------------------------------------ slides
    def render(self, out_path: Path) -> dict[str, Any]:
        self.m01()
        self.m02()
        self.m03()
        self.m04()
        self.m05()
        self.m06()
        self.m07()
        self.m08()
        self.m09()
        self.m10()
        self.m11()
        self.m12()
        self.m13()
        self.m14()
        self.m15()
        self.m16()
        self.m17()
        self.m19()
        self.m20()
        self.m21()
        self.m22()
        self.m18()
        self.a01()
        self.a02()
        if self.fp["slides"].get("A03", {}).get("revisions"):
            self.a03()
        self.a04()
        self.a05()
        self.a06()
        self.d.save(out_path)
        return {"path": str(out_path), "slides": self.slides_index, "n_slides": self.d.page}

    def m01(self):
        d = self.d
        s = d.new_slide()
        C = self.C
        d.add_rect(s, 0, 0, 4.85, 7.5, C["primary"], None, radius=None)
        d.add_rect(s, 4.85, 0, 0.09, 7.5, C["gold"], None, radius=None)
        ed = self.ed
        gen = dt.datetime.fromisoformat(self.fp["generated_at"].replace("Z", "+00:00"))
        d.add_text(s, 0.56, 1.1, 4.0, 0.3, config.term("monthly_edition", self.lang).upper(), size=9, color=C["cover_text"], bold=True)
        d.add_text(s, 0.56, 1.5, 4.1, 1.4, config.term("report_title", self.lang), size=26, bold=True, color=C["white"], line_spacing=1.0)
        bp = plabel(ed.get("banking_period"), "month_end_stock") if ed.get("banking_period") else "n/a"
        mp = plabel(ed.get("macro_period"), "ytd_flow") if ed.get("macro_period") else "n/a"
        cp = plabel(ed.get("cpi_period"), "monthly") if ed.get("cpi_period") else "n/a"
        d.add_text(s, 0.56, 3.0, 4.0, 0.9, f"Edition {ed.get('edition_month')} · banking data to {bp} · macro data to {mp} · CPI {cp}", size=12, color=C["cover_text"], line_spacing=1.05)
        d.add_text(s, 0.6, 5.7, 3.9, 0.9, f"{config.term('draft_label', self.lang)}. {config.term('information_cutoff', self.lang)}: {self.fp['as_of']} 23:59 Asia/Baku. "
                   f"{config.term('generated', self.lang)}: {gen:%d %b %Y %H:%M} UTC. Narrative mode: {self.nar.get('mode')}.", size=9, color=C["cover_text"], line_spacing=1.05)
        d.add_text(s, 0.6, 6.62, 4.0, 0.3, f"{self.settings['report'].get('organisation_label')}  |  {self.settings['report'].get('audience_label')}", size=9, color=C["cover_text"])
        if d.logo_path:
            s.shapes.add_picture(d.logo_path, d.prs.slide_width - d.prs.slide_width * 0.46, d.prs.slide_height * 0.28, width=d.prs.slide_width * 0.19)
        headline = (self.nar.get("cover") or {}).get("headline") or "Facts-only descriptive edition"
        d.add_panel(s, 5.8, 4.32, 6.73, 1.4)
        d.add_text(s, 6.06, 4.42, 6.2, 1.2, headline, size=12, color=C["text"], anchor="m", line_spacing=1.05)
        anc = self.fp["anchors"]
        lines = []
        for role in ("banking", "macro", "prices"):
            a = anc.get(role, {})
            for ds in a.get("datasets", []):
                lines.append(f"{role}: {ds['dataset_id']} — latest period {ds.get('latest_period_end')}, published {ds.get('published_at') or 'n/a'}, {'verified' if ds.get('verified') else 'NOT verified'}")
        d.add_text(s, 5.8, 5.95, 6.9, 0.9, lines, size=8, color=C["muted"], line_spacing=1.0)
        d.add_notes(s, "Cover. Reporting anchors and verification status:\n" + "\n".join(lines) + f"\nFact pack hash {self.fp.get('fact_pack_hash')}. Snapshot and manifest are listed in manifest.json.")
        self.slides_index.append({"id": "M01", "page": d.page, "title": "Cover"})

    def m02(self):
        d = self.d
        s = d.new_slide()
        C = self.C
        d.add_title(s, self._title("M02", "Executive findings: what deserves management attention this month"), "Executive findings",
                    f"Up to five findings ranked by materiality · banking data to {plabel(self.ed.get('banking_period'), 'month_end_stock')} · macro data to {plabel(self.ed.get('macro_period'), 'ytd_flow')}")
        findings = (self.nar.get("findings") or [])[:5]
        y0, card_w, gap = 1.45, 7.9, 0.08
        card_h = (5.3 - gap * 4) / 5
        for i, f in enumerate(findings):
            yy = y0 + i * (card_h + gap)
            d.add_panel(s, 0.45, yy, card_w, card_h)
            col = C["negative"] if f.get("direction") == "adverse" else (C["positive"] if f.get("direction") == "favourable" else C["primary"])
            d.add_rect(s, 0.6, yy + (card_h - 0.4) / 2, 0.4, 0.4, col, None, radius=0.5)
            d.add_text(s, 0.6, yy + (card_h - 0.4) / 2, 0.4, 0.4, str(f.get("rank", i + 1)), size=11, bold=True, color=C["white"], align="c", anchor="m")
            head = f"{f.get('slide_id', '')} · {f.get('classification', '').replace('_', ' ')} · {f.get('status', '')}"
            d.add_text(s, 1.15, yy + 0.06, card_w - 1.3, 0.22, head, size=8, bold=True, color=C["primary"])
            body = f.get("statement", "")
            if f.get("banking_relevance"):
                body += "  Relevance: " + f["banking_relevance"]
            if f.get("caveat"):
                body += "  Caveat: " + f["caveat"]
            d.add_text(s, 1.15, yy + 0.28, card_w - 1.3, card_h - 0.32, _clip(body, 470), size=8.5, color=C["text"], line_spacing=1.0, autofit=True)
        rows = []
        for f in findings:
            for ref in (f.get("metric_refs") or [])[:1]:
                m = self.fp["metrics"].get(ref)
                if m and m.get("latest"):
                    rows.append([f.get("id"), _clip(m.get("label") or ref, 42), _fmt(m), _per(m), _chg(m)])
        rx, rw = 8.55, 4.33
        d.add_text(s, rx, y0, rw, 0.24, "Evidence for each finding (latest value, period, change)", size=9, bold=True, color=C["muted"])
        if rows:
            d.add_table(s, rx, y0 + 0.28, rw, min(2.6, 0.32 * (len(rows) + 1)), ["#", "Metric", "Latest", "Period", "Change"], rows, col_widths=[0.35, 1.7, 0.8, 0.85, 0.85],
                        font_size=7.5, align=["l", "l", "r", "l", "r"])
        note = ("Facts-only edition: findings are the five largest documented scorecard movements, classified as observed facts without interpretation."
                if self.facts_only else "Findings distinguish new developments, revisions and continuing themes; classification per item.")
        d.add_so_what(s, rx, 4.6, rw, 2.15, note, heading="HOW TO READ")
        d.add_footer(s, self._source(["CBA and SSC official tables as cited on the underlying slides"]), d.page)
        d.add_notes(s, "Findings are drawn from the narrative JSON; each is classified (observed fact, interpretation, hypothesis, management question) and linked to a slide and metric refs. "
                       f"Validation: {self.nar.get('validation', {}).get('numbers_checked', 0)} numbers checked, {len(self.nar.get('validation', {}).get('problems', []))} problems.")
        self.slides_index.append({"id": "M02", "page": d.page, "title": "Executive findings"})

    def m03(self):
        d = self.d
        s = d.new_slide()
        C = self.C
        d.add_title(s, self._title("M03", "Scorecard: where conditions are improving, weakening or unchanged"), "Scorecard",
                    "Latest published values with their own reference periods; colour follows the configured direction, not growth per se")
        rows, highlights = [], {}
        for i, r in enumerate(self.fp["slides"]["M03"]["rows"]):
            latest = _fmt(r) if r.get("latest") else "n/a"
            prior = _fmt({"latest": r["prior"], "unit": r.get("unit")}) if r.get("prior") else "n/a"
            ch = r.get("change")
            if r.get("kind") == "pct" and r.get("prior") and r["prior"]["value"]:
                ch_txt = f"{(r['latest']['value'] / r['prior']['value'] - 1) * 100:+.1f}%" if r.get("latest") else "n/a"
            else:
                ch_txt = num(ch, 1, "pp" if r.get("unit") in ("%", "pp") else r.get("unit"), sign=True) if ch is not None else "n/a"
            trend = self._sparkline_text(r.get("sparkline") or [])
            good = r.get("good", "neutral")
            verdict = "—"
            if ch is not None and abs(ch) >= 0.05:
                if good == "neutral":
                    verdict = "↑ change" if ch > 0 else "↓ change"
                else:
                    improving = (ch > 0) == (good == "up")
                    verdict = "improving" if improving else "weakening"
                    highlights[i] = "E8F5F1" if improving else "FBE9E7"
            rows.append([r["row_label"], latest, prior, ch_txt, f"{_per(r)} vs {plabel(r['prior']['period'], r.get('period_type')) if r.get('prior') else 'n/a'}", trend, verdict])
        d.add_table(s, 0.45, 1.45, 12.35, 5.2, ["Indicator", "Latest", "Prior", "Change", "Periods", "13-obs trend", "Reading"], rows,
                    col_widths=[3.6, 1.3, 1.3, 1.2, 2.6, 1.6, 1.2], font_size=9.5, align=["l", "r", "r", "r", "l", "c", "c"], highlight_rows=highlights)
        d.add_footer(s, self._source(["CBA monetary statistics tables 2.6, 2.11, 2.12, 3.2.1; CBA bank overview tables 5.2, 5.3, 5.6; SSC headline table and price bulletin"]), d.page)
        self._notes(s, "M03", ["cba_loans_by_institution", "cba_deposits", "cba_deposits_currency", "cba_rates_new", "cba_bank_pnl", "cba_bank_balance", "cba_bank_npl", "ssc_macro_headline_html", "ssc_price_bulletin"],
                    [r["id"] for r in self.fp["slides"]["M03"]["rows"]], "Trend column: ↑/↓/→ per consecutive observation over the last 13 observations (oldest to newest).")
        self.slides_index.append({"id": "M03", "page": d.page, "title": "Scorecard"})

    @staticmethod
    def _sparkline_text(points: list[list[Any]]) -> str:
        vals = [p[1] for p in points if p[1] is not None]
        if len(vals) < 3:
            return "n/a"
        out = []
        for a, b in zip(vals[:-1], vals[1:]):
            out.append("↑" if b > a + 1e-9 else ("↓" if b < a - 1e-9 else "→"))
        return "".join(out[-12:])

    def m04(self):
        f = self.fp["slides"]["M04"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "GDP real growth, YTD y/y"), _kpi(k[1], "Non-oil-gas GDP, YTD y/y"), _kpi(k[2], "Oil-gas GDP, YTD y/y"),
                _kpi(k[3], "Nominal GDP, YTD"), _kpi(k[4], "Non-oil share of nominal GDP")]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w, "Real growth by edition month, YTD vs same period of previous year (%)")
            cats, ser = self._chart_series(f["chart_ytd_growth"], [self.SC["total"], self.SC["non_oil"], self.SC["oil"]])
            self.d.add_line_chart(s, x, y + 0.25, w, h * 0.55, cats, ser, number_format="0.0")
            rows = []
            for r in f["sector_table"]:
                rows.append([r["sector"].replace("_", " ").title(), _fmt(r), _fmt({"latest": r["prior"], "unit": "%"}) if r.get("prior") else "n/a",
                             num(r.get("change"), 1, "pp", sign=True) if r.get("change") is not None else "n/a", _fmt(r.get("share")) if r.get("share") else "n/a"])
            self._caption(s, x, y + h * 0.58 + 0.2, w, f"Value added by activity: real growth YTD ({_per(k[0])}) and nominal share of GDP")
            self.d.add_table(s, x, y + h * 0.58 + 0.45, w, h * 0.42 - 0.45, ["Activity", "Real growth YTD", "Prior edition", "Change", "Share of GDP"], rows,
                             col_widths=[2.6, 1.4, 1.4, 1.0, 1.3], font_size=9)
        self._standard("M04", "Economic growth and its composition", "Economic growth", f"SSC monthly report, {_per(k[0])} · YTD measures, real terms",
                       kpis, main, ["SSC headline table 'Əsas makroiqtisadi göstəricilər' and monthly report Tables 2–3 (Sosial, iqtisadi inkişaf)"],
                       ["ssc_macro_headline_html", "ssc_monthly_report"], ["ssc.gdp.nonoil_share_nominal", "ssc.gdp.sector.construction.real_growth_ytd"],
                       "YTD growth changes between editions are changes in the cumulative rate; quarterly real GDP (2005 prices) is available in the workbook.")

    def m05(self):
        f = self.fp["slides"]["M05"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "CPI y/y"), _kpi(k[1], "CPI, YTD average"), _kpi(k[2], "CPI m/m"), _kpi(k[3], "Nominal wage, YTD y/y"), _kpi(k[4], "Average nominal wage, YTD"),
                _kpi(k[5], "Real wage growth, estimate")]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w, "CPI inflation, y/y by group (%)")
            cats, ser = self._chart_series(f["chart_cpi"], [self.SC["cpi"], self.SC["gold"] if "gold" in self.SC else self.C["gold"], self.SC["corporates"], self.SC["non_oil"]])
            self.d.add_line_chart(s, x, y + 0.25, w, h * 0.5, cats, ser, number_format="0.0")
            self._caption(s, x, y + h * 0.55, w, "Nominal wage growth (YTD y/y), YTD average CPI and the real wage estimate (%)")
            cats2, ser2 = self._chart_series(f["chart_wage"], [self.SC["wages_nominal"], self.SC["cpi"], self.SC["wages_real"]])
            self.d.add_bar_chart(s, x, y + h * 0.55 + 0.25, w, h * 0.45 - 0.25, cats2, ser2, number_format="0.0", gap_width=80)
        self._standard("M05", "Inflation, income and household purchasing power", "Inflation and income", f"SSC price bulletin ({_per(k[0])}) and headline wage table ({_per(k[3])})",
                       kpis, main, ["SSC 'Qiymətlər və qiymət indeksləri' bulletin (m/m, y/y, YTD sheets); SSC headline table (wages, income, retail)"],
                       ["ssc_price_bulletin", "ssc_macro_headline_html"], ["ssc.cpi.all.yoy", "ssc.cpi.all.ytd", "ssc.real_wage_growth_est"],
                       "Real wage growth is an estimate (nominal YTD wage growth deflated by YTD average CPI) and not a debt-service measure.")

    def m06(self):
        f = self.fp["slides"]["M06"]
        rows = [r for r in f["rows"] if r.get("latest")]
        rows_sorted = sorted(rows, key=lambda r: -(r["latest"]["value"] or 0))
        kpis = [_kpi(r, r["row_label"][:34]) for r in rows_sorted[:2]] + [_kpi(r, r["row_label"][:34]) for r in rows_sorted[-2:]]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w, f"Growth by sector, real YTD y/y (%), current edition vs prior edition")
            cats = [r["row_label"] for r in rows_sorted]
            cur = [r["latest"]["value"] for r in rows_sorted]
            pri = [(r["prior"]["value"] if r.get("prior") else None) for r in rows_sorted]
            self.d.add_bar_chart(s, x, y + 0.25, w, h - 0.25, cats, [{"name": "Current edition", "values": cur, "color": self.SC["current"]}, {"name": "Prior edition", "values": pri, "color": self.SC["prior"]}],
                                 horizontal=True, number_format="0.0", data_labels=True, gap_width=50, overlap=-10)
        self._standard("M06", "Sector activity and momentum", "Sector activity", "SSC headline indicators; output, services turnover and investment are different measures",
                       kpis, main, ["SSC headline table (real growth YTD); SSC monthly report Table 2 for construction value added"],
                       ["ssc_macro_headline_html", "ssc_monthly_report"], ["ssc.gdp.sector.construction.real_growth_ytd"],
                       "Rows mix output (industry, agriculture), services turnover (transport, ICT, retail) and investment; a change between editions is a change in cumulative growth.")

    def m07(self):
        f = self.fp["slides"]["M07"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "Exports, YTD"), _kpi(k[2], "Imports, YTD"), _kpi(k[6], "Trade balance, YTD"), _kpi(k[4], "Non-oil exports, YTD"), _kpi(k[7], "CBA official reserves"),
                _kpi(k[9], "Strategic FX reserves (SSC)")]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.5, f"Exports and imports, same YTD window by year (USD mln)")
            periods, values = align_series(f["chart_trade"])
            cats = [plabel(p, "ytd_flow") for p in periods]
            ser = [{"name": sr["label"], "values": v, "color": c} for sr, v, c in zip(f["chart_trade"], values, [self.SC["total"], self.SC["corporates"]])]
            self.d.add_bar_chart(s, x, y + 0.25, w * 0.5 - 0.1, h - 0.25, cats, ser, number_format="#,##0", data_labels=True, gap_width=60, skip=1)
            self._caption(s, x + w * 0.5, y, w * 0.5, "CBA official foreign reserves, month-end (USD mln)")
            cats2, ser2 = self._chart_series(f["chart_reserves"], [self.SC["total"]])
            self.d.add_line_chart(s, x + w * 0.5, y + 0.25, w * 0.5, h - 0.25, cats2, ser2, number_format="#,##0", legend=False)
        self._standard("M07", "External position and FX conditions", "External position", f"SSC foreign trade ({_per(k[0])}); CBA analytical balance ({_per(k[7])}); stocks and flows on separate panels",
                       kpis, main, ["SSC headline table (foreign trade, strategic reserves); CBA Table 2.2 analytical balance (official reserves)"],
                       ["ssc_macro_headline_html", "cba_analytical_balance"], ["ssc.hl.trade_balance.level", "cba.reserves.official_usd.yoy"],
                       "CBA official reserves are distinct from total strategic FX reserves (SOFAZ included); trade is merchandise trade, not the current account.")

    def m08(self):
        f = self.fp["slides"]["M08"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "Loans to the economy, all CI"), _kpi(k[1], "Loans y/y"), _kpi(k[4], "Bank loans y/y"), _kpi(k[5], "NBCI loans y/y"), _kpi(k[6], "New loans, 3-month sum"),
                _kpi(k[8], "Business loans y/y (banks)")]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.55, "Loan stock (AZN mln) and y/y growth (%)")
            cats, ser = self._chart_series(f["chart_stock"], [self.SC["loans"]])
            self.d.add_bar_chart(s, x, y + 0.25, w * 0.55 - 0.1, h * 0.5, cats, ser, number_format="#,##0", legend=False, gap_width=40)
            cats2, ser2 = self._chart_series(f["chart_yoy"], [self.SC["non_oil"]])
            self.d.add_line_chart(s, x, y + h * 0.5 + 0.3, w * 0.55 - 0.1, h * 0.5 - 0.3, cats2, ser2, number_format="0.0", legend=False)
            self._caption(s, x + w * 0.55, y, w * 0.45, f"Contribution to y/y growth of loans to the economy, pp ({_per(f['total_yoy'])})")
            contribs = [c for c in f["contributions"] if c.get("latest")]
            cats3 = [c["row_label"] for c in contribs]
            vals = [c["latest"]["value"] for c in contribs]
            colors = [self.C["positive"] if (v or 0) >= 0 else self.C["negative"] for v in vals]
            self.d.add_bar_chart(s, x + w * 0.55, y + 0.25, w * 0.45, h - 0.25, cats3, [{"name": "Contribution, pp", "values": vals, "color": self.C["primary"]}],
                                 horizontal=True, number_format="0.0", legend=False, data_labels=True, gap_width=45, point_colors=colors)
        self._standard("M08", "Lending growth and composition", "Lending", f"CBA Tables 2.6, 2.7.1, 2.8 ({_per(k[0])}); all credit institutions unless stated",
                       kpis, main, ["CBA Table 2.6 (loans by institution), 2.7.1 (new loans), 2.8 (sectoral breakdown), 5.4 (bank portfolio)"],
                       ["cba_loans_by_institution", "cba_new_loans_by_maturity", "cba_loans_by_sector", "cba_bank_loan_portfolio"],
                       ["cba.loans.total_ci.yoy", "cba.loans.sector.households.contrib", "cba.loans.sector.other_all.contrib", "cba.loans.sector.overdue_unclassified.contrib"],
                       "Contributions use mutually exclusive components of Table 2.8 (sector columns plus the separately reported overdue balance) and reconcile to total growth.")

    def m09(self):
        f = self.fp["slides"]["M09"]
        pts = [p for p in f["points"] if p["credit_yoy"].get("latest") and p["activity_growth"].get("latest")]
        kpis = []
        for p in sorted(pts, key=lambda p: -abs((p["credit_yoy"]["latest"]["value"] or 0) - (p["activity_growth"]["latest"]["value"] or 0)))[:4]:
            kpis.append((f"{p['credit_yoy']['latest']['value']:+.1f}% / {p['activity_growth']['latest']['value']:+.1f}%", p["label"], "credit y/y vs real activity YTD"))

        def main(s, x, y, w, h):
            self._caption(s, x, y, w, "Nominal loan-stock growth y/y (CBA) vs real value-added growth YTD (SSC); bubble = loan share of real-sector loans")
            bubbles = [{"name": p["label"], "x": p["activity_growth"]["latest"]["value"], "y": p["credit_yoy"]["latest"]["value"],
                        "size": (p["loan_share"]["latest"]["value"] if p.get("loan_share") and p["loan_share"].get("latest") else 1.0), "color": c}
                       for p, c in zip(pts, [self.C["primary"], self.C["positive"], self.C["negative"], self.C["neutral"], self.C["gold"], self.C["series_secondary"], self.C["grey"]])]
            self.d.add_bubble_chart(s, x, y + 0.25, w, h * 0.6, bubbles, x_title="Real activity growth, YTD y/y (%)", y_title="Loan stock growth, y/y (%)")
            rows = [[p["label"], f"{p['credit_yoy']['latest']['value']:+.1f}% ({_per(p['credit_yoy'])})", f"{p['activity_growth']['latest']['value']:+.1f}% ({_per(p['activity_growth'])})",
                     (f"{p['loan_share']['latest']['value']:.1f}%" if p.get("loan_share") and p["loan_share"].get("latest") else "n/a"), (p.get("note") or "")[:70]] for p in pts]
            self.d.add_table(s, x, y + h * 0.63, w, h * 0.37, ["Sector", "Credit y/y (nominal)", "Activity YTD (real)", "Loan share", "Mapping note"], rows,
                             col_widths=[1.5, 1.6, 1.6, 0.9, 3.4], font_size=8, align=["l", "r", "r", "r", "l"])
        self._standard("M09", "Sector credit versus sector activity", "Credit vs activity", "Documented CBA→SSC sector mapping; measures differ in basis (nominal stock vs real flow) and coverage",
                       kpis, main, ["CBA Table 2.8 sectoral loans; SSC monthly report Table 2 (value added by activity, comparable prices)"],
                       ["cba_loans_by_sector", "ssc_monthly_report"], ["cba.loans.sector.agriculture.yoy", "ssc.gdp.sector.agriculture.real_growth_ytd"],
                       "Divergences are questions for sector specialists, not evidence of over-lending: inflation, penetration, seasonality and mapping limits all apply.")

    def m10(self):
        f = self.fp["slides"]["M10"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "NPL balance, banks"), _kpi(k[1], "NPL ratio (published)"), _kpi(k[3], "NPL balance y/y"), _kpi(k[7], "Overdue loans, all CI"), _kpi(k[8], "Overdue ratio, all CI"),
                _kpi(k[9], "Allowance stock / NPL")]
        dy, dm = f.get("decomposition_yoy"), f.get("decomposition_mom")

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.5, "NPL balance, banks (AZN mln, prudential)")
            cats, ser = self._chart_series(f["chart_npl"], [self.SC["npl"]])
            self.d.add_bar_chart(s, x, y + 0.25, w * 0.5 - 0.1, h * 0.55, cats, ser, number_format="#,##0", legend=False, gap_width=40)
            self._caption(s, x + w * 0.5, y, w * 0.5, "NPL ratio (banks) and overdue ratio (all credit institutions), %")
            cats2, ser2 = self._chart_series(f["chart_ratio"], [self.SC["npl"], self.SC["overdue"] if self.SC["overdue"] != self.SC["npl"] else self.C["gold"]])
            ser2[1]["color"] = self.C["gold"]
            self.d.add_line_chart(s, x + w * 0.5, y + 0.25, w * 0.5, h * 0.55, cats2, ser2, number_format="0.0")
            rows = []
            for lbl, dd in (("y/y", dy), ("m/m", dm)):
                if dd:
                    rows.append([lbl, f"{dd['ratio_prior']:.2f}%", f"{dd['ratio_current']:.2f}%", f"{dd['total_change_pp']:+.2f} pp", f"{dd['numerator_effect']:+.2f} pp", f"{dd['denominator_effect']:+.2f} pp"])
            self._caption(s, x, y + h * 0.62, w, "NPL ratio change decomposition (numerator changed first): ratio change = numerator effect + denominator effect")
            if rows:
                self.d.add_table(s, x, y + h * 0.62 + 0.25, w, 0.75, ["Window", "Ratio prior", "Ratio current", "Change", "Numerator effect", "Denominator effect"], rows,
                                 col_widths=[0.9, 1.2, 1.2, 1.2, 1.5, 1.6], font_size=9)
        self._standard("M10", "Asset quality and denominator effects", "Asset quality", f"CBA bank overview Tables 5.4, 5.6 ({_per(k[0])}); CBA Table 2.7 overdue loans; labels kept as published",
                       kpis, main, ["CBA Table 5.6 (NPL structure, prudential), 5.4 (loan portfolio), 5.2 (allowance stock), 2.7 (overdue loans, all credit institutions)"],
                       ["cba_bank_npl", "cba_bank_loan_portfolio", "cba_bank_balance", "cba_loans_by_maturity"], ["cba.bank.npl.decomp_yoy", "cba.loans.overdue_ratio", "cba.bank.allowance_to_npl"],
                       "Overdue loans (all credit institutions) and NPL (banks, prudential) are different concepts; the decomposition is arithmetic and sequence-dependent.")

    def m11(self):
        f = self.fp["slides"]["M11"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "Total deposits, all CI"), _kpi(k[1], "Deposits y/y"), _kpi(k[5], "Household deposits y/y"), _kpi(k[7], "Non-financial corporate y/y"),
                _kpi(k[9], "Household share"), _kpi(k[10], "Non-financial corporate share")]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.55, "Deposits by depositor (AZN mln, stacked)")
            cats, ser = self._chart_series(f["chart_stock"], [self.SC["households"], self.SC["corporates"], self.SC["financial"]])
            self.d.add_bar_chart(s, x, y + 0.25, w * 0.55 - 0.1, h - 0.25, cats, ser, stacked=True, number_format="#,##0", gap_width=30)
            self._caption(s, x + w * 0.55, y, w * 0.45, f"Contribution to y/y deposit growth, pp ({_per(k[1])})")
            contribs = [c for c in f["contributions"] if c.get("latest")]
            cats3 = [c["row_label"] for c in contribs]
            vals = [c["latest"]["value"] for c in contribs]
            colors = [self.C["positive"] if (v or 0) >= 0 else self.C["negative"] for v in vals]
            self.d.add_bar_chart(s, x + w * 0.55, y + 0.25, w * 0.45, h * 0.5, cats3, [{"name": "pp", "values": vals, "color": self.C["primary"]}], horizontal=True, number_format="0.0",
                                 legend=False, data_labels=True, gap_width=45, point_colors=colors)
            self._caption(s, x + w * 0.55, y + h * 0.5 + 0.3, w * 0.45, "y/y growth by depositor (%)")
            cats2, ser2 = self._chart_series(f["chart_yoy"], [self.SC["total"], self.SC["households"], self.SC["corporates"]])
            ser2[0]["color"] = self.C["neutral"]
            self.d.add_line_chart(s, x + w * 0.55, y + h * 0.5 + 0.55, w * 0.45, h * 0.5 - 0.55, cats2, ser2, number_format="0.0")
        self._standard("M11", "Deposit growth and funding sources", "Deposits", f"CBA Table 2.11 ({_per(k[0])}); households, financial and non-financial corporations; all credit institutions",
                       kpis, main, ["CBA Table 2.11 deposits and savings in credit institutions by depositor, currency and maturity"], ["cba_deposits"],
                       ["cba.deposits.total.yoy", "cba.deposits.hh.contrib", "cba.deposits.nfc.contrib", "cba.deposits.fin.contrib"],
                       "Aggregate corporate deposit growth does not establish concentration in particular customers.")

    def m12(self):
        f = self.fp["slides"]["M12"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "FX share, total deposits"), _kpi(k[2], "FX share, household deposits"), _kpi(k[3], "FX share, corporate deposits"), _kpi(k[4], "FX share, loans"),
                _kpi(k[5], "Time-deposit share"), _kpi(k[8], "FX share, bank assets")]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w, "FX shares at current exchange rates (%)")
            cats, ser = self._chart_series(f["chart_fx"], [self.SC["deposits"], self.SC["households"], self.SC["loans"]])
            ser[2]["color"] = self.C["gold"]
            self.d.add_line_chart(s, x, y + 0.25, w, h * 0.52, cats, ser, number_format="0.0")
            self._caption(s, x, y + h * 0.55, w, "Term structure of deposits: time-deposit share (%)")
            cats2, ser2 = self._chart_series(f["chart_term"], [self.SC["total"], self.SC["households"]])
            ser2[0]["color"] = self.C["neutral"]
            self.d.add_line_chart(s, x, y + h * 0.55 + 0.25, w, h * 0.45 - 0.25, cats2, ser2, number_format="0.0")
        self._standard("M12", "Funding mix and dollarisation", "Funding mix", f"CBA Tables 2.11, 2.12, 2.7, 5.2 ({_per(k[0])}); shares at current exchange rates",
                       kpis, main, ["CBA Table 2.12 (deposits by currency), 2.11 (by depositor), 2.7 (loans by currency), 5.2 (bank balance sheet FX columns)"],
                       ["cba_deposits_currency", "cba_deposits", "cba_loans_by_maturity", "cba_bank_balance"], ["cba.deposits.fx_share", "cba.deposits.hh.fx_share", "cba.loans.fx_share", "cba.deposits.time_share"],
                       "FX-share changes are not exchange-rate-adjusted flows and do not establish an open FX position or unhedged borrower exposure.")

    def m13(self):
        f = self.fp["slides"]["M13"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "New AZN loans, avg rate", 1), _kpi(k[1], "New AZN term deposits", 1), _kpi(k[2], "Indicative spread, AZN", 1), _kpi(k[3], "New FX loans, avg rate", 1),
                _kpi(k[4], "New FX term deposits", 1), _kpi(k[5], "Indicative spread, FX", 1)]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.5, "New business, AZN: average rates and spread (% p.a. / pp)")
            cats, ser = self._chart_series(f["chart_azn"], [self.SC["loans"], self.SC["deposits"], self.C["gold"]])
            self.d.add_line_chart(s, x, y + 0.25, w * 0.5 - 0.1, h - 0.25, cats, ser, number_format="0.0")
            self._caption(s, x + w * 0.5, y, w * 0.5, "New business, FX: average rates and spread (% p.a. / pp)")
            cats2, ser2 = self._chart_series(f["chart_fx"], [self.SC["loans"], self.SC["deposits"], self.C["gold"]])
            self.d.add_line_chart(s, x + w * 0.5, y + 0.25, w * 0.5, h - 0.25, cats2, ser2, number_format="0.0")
        self._standard("M13", "Lending rates, deposit rates and pricing spread", "Pricing", f"CBA Table 3.2.1 new business ({_per(k[0])}); rates on outstanding balances in the workbook",
                       kpis, main, ["CBA Table 3.2.1 average interest rates on new term deposits and new loans; Table 3.2 outstanding balances"],
                       ["cba_rates_new", "cba_rates_outstanding"], ["cba.rates.new.spread"],
                       "The spread is an indicative pricing spread across all maturities and credit institutions, not NIM; composition effects can move aggregate rates.")

    def m14(self):
        f = self.fp["slides"]["M14"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "Loans y/y"), _kpi(k[1], "Deposits y/y"), _kpi(k[2], "Loan minus deposit growth"), _kpi(k[3], "Loan-to-deposit ratio"), _kpi(k[7], "Long-term share of loans"),
                _kpi(k[10], "Credit / trailing-4Q GDP")]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.5, "Loans and deposits, y/y growth (%)")
            cats, ser = self._chart_series(f["chart_growth"], [self.SC["loans"], self.SC["deposits"]])
            self.d.add_line_chart(s, x, y + 0.25, w * 0.5 - 0.1, h * 0.5, cats, ser, number_format="0.0")
            self._caption(s, x + w * 0.5, y, w * 0.5, "Loan-to-deposit ratio, all credit institutions (%)")
            cats2, ser2 = self._chart_series(f["chart_ldr"], [self.SC["total"]])
            self.d.add_line_chart(s, x + w * 0.5, y + 0.25, w * 0.5, h * 0.5, cats2, ser2, number_format="0.0", legend=False)
            self._caption(s, x, y + h * 0.55, w, "Maturity structure: long-term share of loans and time-deposit share of deposits (%)")
            cats3, ser3 = self._chart_series(f["chart_maturity"], [self.SC["loans"], self.SC["deposits"]])
            self.d.add_line_chart(s, x, y + h * 0.55 + 0.25, w, h * 0.45 - 0.25, cats3, ser3, number_format="0.0")
        self._standard("M14", "Credit growth, funding growth and maturity structure", "Credit vs funding", f"CBA Tables 2.6, 2.7, 2.11, 2.12 ({_per(k[0])}); gross loans / total deposits, all credit institutions",
                       kpis, main, ["CBA Tables 2.6, 2.7 (maturity), 2.11, 2.12; SSC quarterly GDP (Table 03r) for the credit-to-GDP denominator"],
                       ["cba_loans_by_institution", "cba_loans_by_maturity", "cba_deposits", "cba_deposits_currency", "ssc_gdp_quarterly"], ["cba.ldr", "cba.credit_to_gdp", "cba.loans.long_share"],
                       "Partial maturity buckets do not give a liquidity or repricing gap; bank-specific ALM data are required.")

    def m15(self):
        f = self.fp["slides"]["M15"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "Net profit, YTD"), _kpi(k[1], "Net profit YTD y/y"), _kpi(k[3], "Net interest income, YTD"), _kpi(k[8], "Cost-to-income, YTD"), _kpi(k[9], "ROA, annualised"),
                _kpi(k[10], "ROE, annualised")]
        br = f["bridge"]

        def main(s, x, y, w, h):
            start, end = br.get("start"), br.get("end")
            self._caption(s, x, y, w * 0.58, f"Net profit bridge, YTD {plabel(start['period'], 'ytd_flow') if start else ''} → YTD {plabel(end['period'], 'ytd_flow') if end else ''} (AZN mln)")
            steps = []
            if start and end and all(b["contribution"] is not None for b in br["items"]):
                steps.append({"label": f"YTD {plabel(start['period'], 'ytd_flow')}", "value": start["value"], "kind": "total"})
                for b in br["items"]:
                    steps.append({"label": b["label"], "value": b["contribution"], "kind": "delta"})
                if br.get("residual") is not None and abs(br["residual"]) > 0.5:
                    steps.append({"label": "Other/residual", "value": br["residual"], "kind": "delta"})
                steps.append({"label": f"YTD {plabel(end['period'], 'ytd_flow')}", "value": end["value"], "kind": "total"})
                self.d.add_waterfall(s, x, y + 0.25, w * 0.58 - 0.1, h - 0.25, steps)
            else:
                self.d.add_text(s, x, y + 0.4, w * 0.58, 1.0, "Profit bridge not available: one or more P&L components missing for the comparison period.", size=10, color=self.C["muted"])
            self._caption(s, x + w * 0.58, y, w * 0.42, "Net profit, monthly (derived from YTD, January reset; AZN mln)")
            cats, ser = self._chart_series(f["chart_profit"], [self.SC["total"]])
            self.d.add_bar_chart(s, x + w * 0.58, y + 0.25, w * 0.42, h * 0.5, cats, ser, number_format="#,##0", legend=False, gap_width=40)
            self._caption(s, x + w * 0.58, y + h * 0.5 + 0.3, w * 0.42, "Net profit, YTD (AZN mln)")
            cats2, ser2 = self._chart_series(f["chart_ytd"], [self.SC["corporates"]])
            self.d.add_bar_chart(s, x + w * 0.58, y + h * 0.5 + 0.55, w * 0.42, h * 0.5 - 0.55, cats2, ser2, number_format="#,##0", legend=False, gap_width=40)
        self._standard("M15", "Banking-sector profitability and efficiency", "Profitability", f"CBA Table 5.3 prudential P&L, YTD ({_per(k[0])}); bridge reconciles components to the change in net profit",
                       kpis, main, ["CBA Table 5.3 profit and loss statement of the banking sector (YTD); Table 5.2 for average assets and capital"],
                       ["cba_bank_pnl", "cba_bank_balance"], ["cba.bank.pnl.cost_to_income", "cba.bank.roa_annualised", "cba.bank.roe_annualised", "cba.bank.pnl.net_profit.monthly"],
                       "ROA/ROE annualise YTD profit (×12/m) over the average of month-end balances since the previous December; prudential figures, not IFRS.")

    def m16(self):
        f = self.fp["slides"]["M16"]
        k = f["kpis"]
        kpis = [_kpi(k[0], "Total capital (book)"), _kpi(k[2], "Capital / assets (book)"), _kpi(k[3], "Total assets"), _kpi(k[6], "Liquid assets / assets (book)"), _kpi(k[7], "FX share of assets"),
                _kpi(k[10], "Number of banks", 0)]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w * 0.5, "Total capital, banks (AZN mln, prudential balance sheet)")
            cats, ser = self._chart_series(f["chart_capital"], [self.SC["total"]])
            self.d.add_bar_chart(s, x, y + 0.25, w * 0.5 - 0.1, h - 0.25, cats, ser, number_format="#,##0", legend=False, gap_width=40)
            self._caption(s, x + w * 0.5, y, w * 0.5, "Book ratios (%): capital / assets and liquid assets / assets")
            cats2, ser2 = self._chart_series(f["chart_ratios"], [self.SC["total"], self.SC["non_oil"]])
            self.d.add_line_chart(s, x + w * 0.5, y + 0.25, w * 0.5, h - 0.25, cats2, ser2, number_format="0.0")
        self._standard("M16", "Capital and liquidity: published book measures", "Capital and liquidity", f"CBA Table 5.2 consolidated balance sheet ({_per(k[0])}); regulatory ratios not published in these tables",
                       kpis, main, ["CBA Table 5.2 overview of the banking sector (balance sheet, prudential reporting); Table 5.1 participants"],
                       ["cba_bank_balance", "cba_bank_participants"], ["cba.bank.equity_to_assets", "cba.bank.liquid_assets_ratio", "cba.bank.liquid_assets"],
                       "Book measures only: not regulatory capital adequacy, LCR or NSFR; an aggregate does not establish the position of any single bank.")

    def m17(self):
        f = self.fp["slides"]["M17"]
        rows = f["rows"]
        nat = f["national"]
        baku = next((r for r in rows if r["region"].startswith("Bakı")), None)
        rest_l = (nat["loans"] - baku["loans"]) if (nat.get("loans") and baku and baku.get("loans")) else None
        rest_d = (nat["deposits"] - baku["deposits"]) if (nat.get("deposits") and baku and baku.get("deposits")) else None
        def _pct(v):
            return f"{v:.1f}%" if isinstance(v, (int, float)) else "n/a"
        kpis = [(money(nat.get("loans")), "Bank loans by region, total", plabel(nat.get("period"), "month_end_stock")), (money(nat.get("deposits")), "Household savings by region, total", plabel(nat.get("period"), "month_end_stock")),
                (f"{_pct(baku.get('loan_share'))} / {_pct(baku.get('deposit_share'))}" if baku else "n/a", "Baku share: loans / savings", "booking-region basis"),
                (f"{money(rest_l)} / {money(rest_d)}" if (rest_l is not None and rest_d is not None) else "n/a", "Rest of Azerbaijan: loans / savings", plabel(nat.get("period"), "month_end_stock"))]

        def main(s, x, y, w, h):
            self._caption(s, x, y, w, "Loans and household savings by economic region, share of national total (%), excluding Baku")
            rr = [r for r in rows if not r["region"].startswith("Bakı") and r.get("loan_share") is not None]
            cats = [r["region"].replace(" iqtisadi rayonu", "") for r in rr]
            self.d.add_bar_chart(s, x, y + 0.25, w * 0.55 - 0.1, h - 0.25, cats, [{"name": "Loan share", "values": [r["loan_share"] for r in rr], "color": self.SC["loans"]},
                                                                                {"name": "Savings share", "values": [r["deposit_share"] for r in rr], "color": self.SC["deposits"]}],
                                 horizontal=True, number_format="0.0", data_labels=True, gap_width=40, overlap=-10)
            trows = [[r["region"].replace(" iqtisadi rayonu", ""), f"{r['loans']:,.0f}" if r["loans"] is not None else "n/a", f"{r['loan_share']:.1f}%" if r["loan_share"] is not None else "n/a",
                      f"{r['deposits']:,.0f}" if r["deposits"] is not None else "n/a", f"{r['deposit_share']:.1f}%" if r["deposit_share"] is not None else "n/a",
                      f"{r['ldr']:.0f}%" if r["ldr"] is not None else "n/a"] for r in rows]
            self.d.add_table(s, x + w * 0.55, y + 0.25, w * 0.45, h - 0.25, ["Region", "Loans, mln", "Share", "Savings, mln", "Share", "Loans/savings"], trows,
                             col_widths=[1.7, 0.9, 0.6, 0.9, 0.6, 0.8], font_size=7.5)
        self._standard("M17", "Regional lending and deposits", "Regions", f"CBA Tables 2.10 and 2.14, latest month ({plabel(nat.get('period'), 'month_end_stock')}); booking region, banks",
                       kpis, main, ["CBA Table 2.10 loans by regions (banks, thousand manats); Table 2.14 savings by regions (household savings)"],
                       ["cba_loans_regions", "cba_deposits_regions"], [],
                       f.get("note", ""))

    def m18(self):
        d = self.d
        s = d.new_slide()
        C = self.C
        d.add_title(s, self._title("M18", "Management questions and next releases"), "Management questions", "Public signal → why it might matter → internal information needed → next observation to watch")
        qs = (self.nar.get("questions") or [])[:3]
        cw = (12.35 - 0.15 * (len(qs) - 1)) / max(1, len(qs))
        for i, q in enumerate(qs):
            x = 0.45 + i * (cw + 0.15)
            d.add_rect(s, x, 1.45, cw, 0.95, C["primary"], None, radius=0.03)
            d.add_text(s, x + 0.15, 1.5, cw - 0.3, 0.85, f"{i+1}. {q.get('question', '')}", size=9.5, bold=True, color=C["white"], anchor="m", autofit=True)
            d.add_panel(s, x, 2.4, cw, 2.3)
            body = [[{"text": "Signal: ", "bold": True, "size": 9}, {"text": q.get("signal", ""), "size": 9}],
                    [{"text": "Why it matters: ", "bold": True, "size": 9}, {"text": q.get("why", ""), "size": 9}],
                    [{"text": "Internal information: ", "bold": True, "size": 9}, {"text": q.get("internal_data", ""), "size": 9}],
                    [{"text": "Watch next: ", "bold": True, "size": 9}, {"text": q.get("watch", ""), "size": 9}],
                    [{"text": "Function: ", "bold": True, "size": 9}, {"text": q.get("function", ""), "size": 9}]]
            d.add_text(s, x + 0.15, 2.5, cw - 0.3, 2.15, body, size=9, color=C["text"], space_after=3, line_spacing=1.0, autofit=True)
        nxt = self.fp["slides"]["M18"]["next_releases"][:7]
        rows = [[r["title"][:60], r["source"], plabel(r["next_period"], "month_end_stock"), r["expected_by"], r.get("status", "expected (schedule), not confirmed")] for r in nxt]
        d.add_text(s, 0.45, 4.85, 12.35, 0.25, "Next expected releases (official schedule: 'within 30 days after the reporting period'; dates are expectations, not confirmed publications)", size=9, bold=True, color=C["muted"])
        d.add_table(s, 0.45, 5.12, 12.35, 1.6, ["Dataset", "Source", "Next period", "Expected by", "Status"], rows, col_widths=[5.2, 1.4, 1.6, 1.5, 2.6], font_size=8.5, align=["l", "l", "l", "l", "l"])
        d.add_footer(s, self._source(["CBA schedule of publication of statistical reports; SSC release practice"]), d.page)
        d.add_notes(s, "Questions are proportionate (investigate, compare, monitor); no named individuals; no approval is implied. Expected dates derive from configured expected_lag_days in config/sources.yaml.")
        self.slides_index.append({"id": "M18", "page": d.page, "title": "Management questions"})

    def a01(self):
        d = self.d
        s = d.new_slide()
        d.add_title(s, "A01 · Metric definitions, formulas and coverage", "Appendix", "Deterministic formulas from config/metrics.yaml; institutional population and period type per metric")
        defs = [x for x in self.fp["definitions"] if x["id"] in {
            "cba.loans.total_ci.yoy", "cba.deposits.total.yoy", "cba.deposits.fx_share", "cba.loans.fx_share", "cba.ldr", "cba.loans.overdue_ratio", "cba.bank.npl.ratio_calc",
            "cba.bank.npl.decomp_yoy", "cba.rates.new.spread", "cba.bank.pnl.cost_to_income", "cba.bank.roa_annualised", "cba.bank.equity_to_assets", "cba.bank.liquid_assets_ratio",
            "ssc.real_wage_growth_est", "ssc.cpi.all.yoy", "cba.credit_to_gdp", "cba.loans.sector.households.contrib", "cba.bank.pnl.net_profit.monthly"}]
        rows = [[x["id"], _clip(x.get("label") or "", 78), x["formula"], x.get("unit") or "", _clip(x.get("basis") or "", 70)] for x in defs][:15]
        d.add_table(s, 0.45, 1.45, 12.35, 5.1, ["Metric id", "Definition", "Formula", "Unit", "Basis / caveat"], rows, col_widths=[2.5, 4.6, 1.3, 0.6, 3.35], font_size=7.5, align=["l", "l", "l", "l", "l"], row_height=0.3)
        d.add_footer(s, "Full dictionary with inputs and period types: workbook sheet 'Definitions'.", d.page)
        d.add_notes(s, "Growth = (current / comparable prior − 1) × 100; contribution = Δcomponent / prior total × 100; pp change = current % − prior %; real wage estimate = [(1+n)/(1+p) − 1] × 100; "
                       "NPL decomposition: numerator effect 100·(Q1−Q0)/L0, denominator effect 100·Q1·(1/L1−1/L0).")
        self.slides_index.append({"id": "A01", "page": d.page, "title": "Definitions"})

    def a02(self):
        d = self.d
        s = d.new_slide()
        d.add_title(s, "A02 · Source and freshness register", "Appendix", f"Datasets, latest reference period, publication date basis and collection status as of {self.fp['as_of']}")
        reg = self.fp["slides"]["A02"]["register"]
        rows = []
        for r in reg:
            if r["role"] == "reference" and not r.get("latest_period_end"):
                continue
            rows.append([r["dataset_id"], (r.get("title_en") or "")[:58], r["source_id"], r.get("latest_period_end") or "—", r.get("published_at") or "n/a", (r.get("status") or "not collected")[:22], r.get("role")])
        d.add_table(s, 0.45, 1.45, 12.35, 5.25, ["Dataset", "Title", "Source", "Latest period", "Published", "Status", "Role"], rows[:32],
                    col_widths=[2.3, 4.6, 1.3, 1.1, 1.0, 1.6, 0.9], font_size=6.5, align=["l", "l", "l", "l", "l", "l", "l"], row_height=0.155)
        miss = self.fp["availability"]["unverified"]
        lower = [u for u in miss if not isinstance(u, str) and u["status"] != "unverified"]
        none_ = [u if isinstance(u, str) else u["item"] for u in miss if isinstance(u, str) or u["status"] == "unverified"]
        parts = []
        if lower:
            parts.append("Published only at a lower frequency: " + "; ".join(
                f'{u["item"]} ({u["value"]:g}% at {u["latest_period"]}, from the Financial Stability Report)' for u in lower))
        if none_:
            parts.append("Not published in the collected sources: " + "; ".join(none_))
        d.add_footer(s, " · ".join(parts), d.page)
        d.add_notes(s, "Publication dates: 'title_date' = date in the CBA link title; 'page_news_date' = SSC page date; n/a = not explicitly available (not inferred). Full register in the workbook.")
        self.slides_index.append({"id": "A02", "page": d.page, "title": "Source register"})

    def a03(self):
        d = self.d
        s = d.new_slide()
        d.add_title(s, "A03 · Revisions since the prior report", "Appendix", "New vintages replacing previously stored observations (workflow materiality thresholds, not risk limits)")
        revs = self.fp["slides"]["A03"]["revisions"]
        rows = []
        for v in revs[:14]:
            ex = v.get("examples") or []
            first = ex[0] if ex else {}
            rows.append([v["dataset_id"], v["created_at"][:10], str(v["n_revisions"]), first.get("series_id", ""), first.get("period_end", ""), f"{first.get('old')}" if first else "", f"{first.get('new')}" if first else ""])
        d.add_table(s, 0.45, 1.45, 12.35, 5.0, ["Dataset", "Vintage date", "# revised", "Example series", "Period", "Old", "New"], rows, col_widths=[2.4, 1.2, 0.9, 3.4, 1.2, 1.6, 1.6], font_size=8, align=["l", "l", "r", "l", "l", "r", "r"])
        d.add_footer(s, "Append-only observation vintages; prior values remain stored and are listed in the workbook sheet 'Revisions'.", d.page)
        self.slides_index.append({"id": "A03", "page": d.page, "title": "Revisions"})

    def a04(self):
        d = self.d
        s = d.new_slide()
        d.add_title(s, "A04 · Additional time series used in the analysis", "Appendix", f"Latest {self.settings.get('chart_window', 36)} observations; longer history in the workbook")
        series = self.fp["slides"]["A04"]["series"]
        cw, ch = 4.0, 2.45
        for i, sr in enumerate(series[:6]):
            col, row = i % 3, i // 3
            x, y = 0.45 + col * (cw + 0.17), 1.45 + row * (ch + 0.25)
            cats = month_labels([p[0] for p in sr["points"]], self.lang)
            vals = [p[1] for p in sr["points"]]
            self._caption(s, x, y, cw, f"{sr['label']} ({sr.get('unit') or ''})")
            d.add_line_chart(s, x, y + 0.22, cw, ch - 0.22, cats, [{"name": sr["label"], "values": vals, "color": self.C["primary"]}], number_format="#,##0.0", legend=False, skip=6)
        d.add_footer(s, self._source(["CBA and SSC tables as listed in A02"]), d.page)
        self.slides_index.append({"id": "A04", "page": d.page, "title": "Additional series"})


def render_monthly(fp: dict[str, Any], nar: dict[str, Any], out_path: Path, lang: str = "en") -> dict[str, Any]:
    theme = dict(config.theme())
    theme["_root"] = str(config.ROOT)
    r = MonthlyRenderer(fp, nar, theme, lang)
    return r.render(out_path)
