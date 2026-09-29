"""Fact pack builder: validated observations, computed metrics, changes, chart data and evidence ids.

The fact pack is the only numeric input to narrative generation and rendering. Every number
shown on a slide is taken from here (and mirrored in the workbook), never recomputed in prose.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from . import config
from .calc.data import ObservationStore
from .calc.metrics import MetricEngine, MetricResult
from .calc.validate import validate_all
from .storage.db import Database, utcnow
from .util.periods import EN_MONTHS, parse_as_of, period_label, shift_months, today_baku


# Series dated by the event that set them rather than by a calendar period. A lag of twelve months
# lands on no observation, so these compare against the preceding entry in the series.
EVENT_DATED_TYPES = {"policy_rate_effective"}


def _rationale_sentences(text: str | None) -> list[str]:
    """Substantive sentences of a decision statement, past the dateline boilerplate."""
    if not text:
        return []
    body = re.sub(r"^.*?\b(Baku|Bakı)\s*:\s*", "", text.strip(), count=1, flags=re.IGNORECASE | re.DOTALL)
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", body) if len(x.strip()) > 30]


def _rationale_gist(text: str | None) -> str:
    """The substantive opening of a decision statement, past the dateline boilerplate."""
    if not text:
        return "not available"
    sentences = _rationale_sentences(text)
    body = re.sub(r"^.*?\b(Baku|Bakı)\s*:\s*", "", (text or "").strip(), count=1, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(sentences[:2])[:260] or body[:260]


def _rationale_difference(previous: str | None, current: str | None) -> tuple[str, str]:
    """What each statement says that the other does not.

    Two decision statements open with the same formula, so quoting the first sentences of each
    would show the reader two identical cells under a heading that claims the reasoning changed.
    The sentences they share are dropped and what is left is what actually differs.
    """
    prev_s, cur_s = _rationale_sentences(previous), _rationale_sentences(current)
    if not prev_s and not cur_s:
        return "not available", "not available"
    norm = lambda s: re.sub(r"[^a-zçəğıöşü0-9 ]", "", s.lower()).strip()
    shared = {norm(s) for s in prev_s} & {norm(s) for s in cur_s}
    only_prev = [s for s in prev_s if norm(s) not in shared]
    only_cur = [s for s in cur_s if norm(s) not in shared]
    if not only_prev and not only_cur:
        same = "same wording as the other statement"
        return same, same
    def fmt(xs: list[str], other: str) -> str:
        if not xs:
            return f"nothing the {other} statement does not also say"
        out = ""
        for s in xs:                                  # whole sentences only, so nothing is cut mid-clause
            if len(out) + len(s) + 1 > 175:
                break
            out = f"{out} {s}".strip()
        return out or (xs[0][:172].rsplit(" ", 1)[0] + " …")
    return fmt(only_prev, "current"), fmt(only_cur, "previous")


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    return float(v)


class FactPackBuilder:
    def __init__(self, db: Database, as_of: dt.date, lang: str = "en", historical: bool = False):
        self.db = db
        self.as_of = as_of
        self.lang = lang
        self.cutoff_iso = dt.datetime.combine(as_of, dt.time(23, 59, 59)).isoformat()
        self.store = ObservationStore(db, self.cutoff_iso, historical_information_set=historical)
        self.eng = MetricEngine(self.store, cutoff=as_of)
        self.eng.compute_all()
        self.settings = config.settings()
        self.rcfg = config.reports_config()["monthly"]
        self.window = int(self.settings.get("chart_window", 36))
        self.sources_used: dict[str, dict[str, Any]] = {}
        self.metric_refs: dict[str, dict[str, Any]] = {}
        self.approved_numbers: list[dict[str, Any]] = []
        self._pubs: dict[str, Any] | None = None
        self.states = {k: dict(v) for k, v in db.dataset_states().items()}

    # ------------------------------------------------------------------ helpers
    def _default_dims(self, sid: str, dims: dict[str, str] | None) -> dict[str, str] | None:
        if not dims and sid in self.eng._by_id and self.eng._by_id[sid].get("dims"):
            return dict(self.eng._by_id[sid]["dims"])
        return dims

    def _series(self, sid: str, dims: dict[str, str] | None = None) -> pd.Series:
        dims = self._default_dims(sid, dims)
        r = self.eng.get(sid, dims)
        if r is not None:
            return r.series
        if sid in self.eng._by_id:
            try:
                self.eng.compute(sid, dims)
            except Exception:
                return pd.Series(dtype=float)
            r = self.eng.get(sid, dims)
            return r.series if r is not None else pd.Series(dtype=float)
        return self.store.series(sid, dims, cutoff=self.as_of)

    def _meta(self, sid: str, dims: dict[str, str] | None = None) -> dict[str, Any]:
        dims = self._default_dims(sid, dims)
        r = self.eng.get(sid, dims)
        if r is not None:
            return {"kind": "metric", "unit": r.unit, "label": r.label_en or sid, "period_type": r.period_type, "formula": r.formula,
                    "basis": r.basis, "inputs": r.inputs}
        info = self.store.info(sid, dims)
        reg = self.store.registry.get(sid, {})
        if info is None:
            return {"kind": "series", "unit": reg.get("unit"), "label": reg.get("label_en") or sid, "period_type": reg.get("period_type"), "doc_ids": []}
        for d in info.doc_ids:
            if d in self.store.docs:
                self.sources_used[d] = self.store.docs[d]
        return {"kind": "series", "unit": info.unit, "label": reg.get("label_en") or info.label_original or sid, "period_type": info.period_type,
                "population": info.population, "basis": info.basis, "doc_ids": info.doc_ids, "dataset_id": info.dataset_id,
                "source_id": info.source_id, "published_at": info.latest_published_at}

    def _register_inputs(self, sid: str, dims: dict[str, str] | None = None) -> list[str]:
        """Collect evidence doc ids for a metric by walking its inputs."""
        dims = self._default_dims(sid, dims)
        r = self.eng.get(sid, dims)
        docs: list[str] = []
        if r is None:
            m = self._meta(sid, dims)
            return list(m.get("doc_ids", []))
        for i in r.inputs:
            if "series_id" in i:
                docs += i.get("doc_ids", [])
                for d in i.get("doc_ids", []):
                    if d in self.store.docs:
                        self.sources_used[d] = self.store.docs[d]
            elif "metric_id" in i:
                docs += self._register_inputs(i["metric_id"], i.get("dims") or None)
        return sorted(set(docs))

    def snap(self, sid: str, dims: dict[str, str] | None = None, period: dt.date | None = None, compare: str = "lag12", key: str | None = None) -> dict[str, Any]:
        """Latest value at or before `period` plus a comparable prior value and change."""
        s = self._series(sid, dims)
        meta = self._meta(sid, dims)
        out: dict[str, Any] = {"id": sid, "dims": dims or {}, "label": meta.get("label"), "unit": meta.get("unit"), "period_type": meta.get("period_type"),
                               "basis": meta.get("basis"), "latest": None, "prior": None, "change": None, "compare": compare, "available": False}
        if s.empty:
            out["missing_reason"] = "no observations"
            return out
        if period is not None:
            s2 = s[s.index <= period]
            if s2.empty:
                out["missing_reason"] = f"no observation at or before {period}"
                return out
            s = s2
        d = s.index[-1]
        out["latest"] = {"period": d.isoformat(), "value": _f(s.iloc[-1]), "period_label": period_label(d, meta.get("period_type") or "", self.lang)}
        out["available"] = True
        # a decision-dated series has no month twelve months back to compare with: meetings are
        # irregular, so the comparison a reader wants is the decision before this one
        if (meta.get("period_type") or "") in EVENT_DATED_TYPES and compare in ("lag12", "lag1", "prior_edition"):
            compare = "previous_observation"
            out["compare"] = compare
        if compare == "lag12":
            pd_ = shift_months(d, -12)
        elif compare == "lag1":
            pd_ = shift_months(d, -1)
        elif compare == "prior_edition":
            pd_ = shift_months(d, -1)
        else:
            pd_ = None
        if pd_ is not None and pd_ in s.index:
            out["prior"] = {"period": pd_.isoformat(), "value": _f(s[pd_]), "period_label": period_label(pd_, meta.get("period_type") or "", self.lang)}
            if out["latest"]["value"] is not None and out["prior"]["value"] is not None:
                out["change"] = out["latest"]["value"] - out["prior"]["value"]
        elif compare in ("prior_edition", "previous_observation") and len(s) >= 2:
            pd_ = s.index[-2]
            out["prior"] = {"period": pd_.isoformat(), "value": _f(s.iloc[-2]), "period_label": period_label(pd_, meta.get("period_type") or "", self.lang)}
            out["change"] = out["latest"]["value"] - out["prior"]["value"] if out["latest"]["value"] is not None else None
        out["doc_ids"] = self._register_inputs(sid, dims)
        ref = key or (sid + (("|" + json.dumps(dims, sort_keys=True)) if dims else ""))
        self.metric_refs[ref] = out
        self.approved_numbers.append({"ref": ref, "value": out["latest"]["value"], "period": out["latest"]["period"], "unit": out["unit"]})
        if out["prior"]:
            self.approved_numbers.append({"ref": ref + ".prior", "value": out["prior"]["value"], "period": out["prior"]["period"], "unit": out["unit"]})
        if out["change"] is not None:
            self.approved_numbers.append({"ref": ref + ".change", "value": out["change"], "period": out["latest"]["period"], "unit": "pp" if (out["unit"] in ("%", "pp")) else out["unit"]})
        return out

    def chart_series(self, sid: str, dims: dict[str, str] | None = None, window: int | None = None, end: dt.date | None = None, label: str | None = None,
                     keep_months: list[int] | None = None) -> dict[str, Any]:
        s = self._series(sid, dims)
        meta = self._meta(sid, dims)
        if end is not None:
            s = s[s.index <= end]
        if keep_months:
            s = s[[i for i in s.index if i.month in keep_months]]
        s = s.dropna().tail(window or self.window)
        self._register_inputs(sid, dims)
        return {"id": sid, "dims": dims or {}, "label": label or meta.get("label") or sid, "unit": meta.get("unit"), "period_type": meta.get("period_type"),
                "points": [[d.isoformat(), _f(v)] for d, v in s.items()]}

    # ------------------------------------------------------------------ anchors / availability
    def anchors(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for role, datasets in self.rcfg["anchors"].items():
            periods = []
            for dsid in datasets:
                rows = self.store.df[self.store.df["dataset_id"] == dsid]
                latest = max(rows["period_end"]) if not rows.empty else None
                docs = sorted(set(rows["doc_id"])) if not rows.empty else []
                pub = None
                for d in docs:
                    p = (self.store.docs.get(d) or {}).get("published_at")
                    if p and (pub is None or p > pub):
                        pub = p
                st = self.states.get(dsid, {})
                periods.append({"dataset_id": dsid, "latest_period_end": latest.isoformat() if latest else None, "published_at": pub,
                                "status": st.get("status"), "verified": bool(latest) and (st.get("status") or "").startswith(("parsed", "unchanged"))})
            common = None
            if all(p["latest_period_end"] for p in periods):
                common = min(p["latest_period_end"] for p in periods)
            out[role] = {"datasets": periods, "period_end": common, "verified": all(p["verified"] for p in periods)}
        # CPI period: latest month with a y/y index
        cpi = self._series("ssc.cpi.all.yoy")
        out["prices"]["period_end"] = cpi.index[-1].isoformat() if not cpi.empty else out["prices"].get("period_end")
        return out

    def _elsewhere(self, item: str, series_id: str, where: str) -> dict[str, Any]:
        """An input the monthly tables do not carry, but another publication does."""
        hit = self.store.latest_any(series_id)
        if hit is None:
            return {"item": item, "status": "unverified", "note": f"expected in {where}, but nothing is held"}
        return {"item": item, "status": "available at a lower frequency", "series_id": series_id,
                "latest_period": hit[0].isoformat(), "value": hit[1], "note": where}

    def availability_matrix(self) -> dict[str, Any]:
        """Slide-input availability: available / partial / unavailable / unverified."""
        needs = {
            "M04": ["ssc.hl.gdp.growth", "ssc.hl.gdp_oil.growth", "ssc.hl.gdp_nonoil.growth", "ssc.gdp.sector.total.real_index_ytd"],
            "M05": ["ssc.cpi.all.yoy", "ssc.cpi.all.ytd", "ssc.hl.wage.growth", "ssc.real_wage_growth_est", "ssc.hl.retail.growth"],
            "M06": ["ssc.hl.agriculture.growth", "ssc.hl.industry_nonoil.growth", "ssc.hl.transport.growth", "ssc.hl.ict.growth", "ssc.hl.retail.growth", "ssc.hl.investment.growth", "ssc.gdp.sector.construction.real_growth_ytd"],
            "M07": ["ssc.hl.exports.level", "ssc.hl.imports.level", "ssc.hl.exports_nonoil.level", "cba.reserves.official_usd", "ssc.hl.strategic_reserves.level"],
            "M08": ["cba.loans.total_ci", "cba.loans.total_ci.yoy", "cba.loans.sector.households.contrib", "cba.new_loans.total"],
            "M09": ["cba.loans.sector.agriculture.yoy", "ssc.gdp.sector.agriculture.real_growth_ytd"],
            "M10": ["cba.bank.npl.total", "cba.bank.npl.ratio", "cba.bank.npl.decomp_yoy", "cba.loans.overdue_ratio"],
            "M11": ["cba.deposits.total", "cba.deposits.hh.contrib", "cba.deposits.nfc.contrib"],
            "M12": ["cba.deposits.fx_share", "cba.deposits.hh.fx_share", "cba.loans.fx_share", "cba.deposits.time_share"],
            "M13": ["cba.rates.new.loan", "cba.rates.new.deposit", "cba.rates.new.spread"],
            "M14": ["cba.loans.total_ci.yoy", "cba.deposits.total.yoy", "cba.ldr", "cba.loans.long_share"],
            "M15": ["cba.bank.pnl.net_profit", "cba.bank.pnl.net_interest_income", "cba.bank.pnl.provisions", "cba.bank.pnl.cost_to_income", "cba.bank.roa_annualised"],
            "M16": ["cba.bank.capital.total", "cba.bank.equity_to_assets", "cba.bank.liquid_assets_ratio", "cba.bank.fx_assets_share"],
            "M17": ["cba.loans.region.total", "cba.hh_savings.region.total"],
            "M19": ["cba.policy.rate", "cba.policy.corridor_floor", "cba.policy.corridor_ceiling"],
            "M20": ["ssc.cpi.all.yoy", "cba.forecast.inflation"],
            "M21": ["cba.fsr.car", "cba.fsr.lcr", "cba.fsr.npl_ratio", "cba.fsr.roa", "cba.fsr.roe"],
            "M22": ["cba.fsr.stress.car", "cba.fsr.deposit_dollarisation"],
        }
        # Inputs the monthly statistical tables do not publish. Some of them are published elsewhere
        # at a lower frequency, and saying only "unverified" would now be wrong: the entry names the
        # publication that carries it, its frequency and the reporting date actually held.
        unverified = [
            self._elsewhere("Regulatory capital adequacy ratio", "cba.fsr.car",
                            "Financial Stability Report, annual and half-yearly; not in the monthly tables"),
            self._elsewhere("Liquidity coverage ratio (LCR)", "cba.fsr.lcr",
                            "Financial Stability Report, annual and half-yearly; not in the monthly tables"),
            {"item": "NSFR (published sector aggregate)", "status": "unverified",
             "note": "not found in the monthly tables or in the stability reports collected"},
            {"item": "Balance of payments / current account", "status": "unverified",
             "note": "CBA publishes with about a 90-day lag; not collected"},
            {"item": "CBA FX interventions", "status": "unverified", "note": "not published in the collected tables"},
            {"item": "Stage 3 / IFRS 9 sector aggregates", "status": "unverified", "note": "not published in the collected tables"},
            {"item": "Real wage index (official)", "status": "unverified",
             "note": "not published; the deck shows an estimate derived from nominal wages and CPI, labelled as such"},
            {"item": "Regional population for per-capita measures", "status": "unverified", "note": "not collected"},
        ]
        matrix = {}
        missing = []
        for slide, ids in needs.items():
            rows = []
            for sid in ids:
                dims = None
                if sid.startswith("cba.rates.new"):
                    dims = {"currency": "AZN"}
                if sid.startswith("cba.loans.region") or sid.startswith("cba.hh_savings.region"):
                    dims = {"region": "national"}
                if sid in ("cba.bank.capital.total",):
                    dims = {"currency": "all"}
                if sid.startswith(("cba.fsr.", "cba.forecast.", "cba.policy.")):
                    # scenario, vintage and exercise change with every edition, so availability asks
                    # whether the series is published at all rather than whether one slice resolves
                    hit = self.store.latest_any(sid)
                    rows.append({"input": sid, "status": "available" if hit else "unavailable",
                                 "latest_period": hit[0].isoformat() if hit else None})
                    if not hit:
                        missing.append(f"{slide}:{sid}")
                    continue
                s = self._series(sid, dims)
                ok = not s.empty
                rows.append({"input": sid, "status": "available" if ok else "unavailable", "latest_period": s.index[-1].isoformat() if ok else None})
                if not ok:
                    missing.append(f"{slide}:{sid}")
            n_ok = sum(1 for r in rows if r["status"] == "available")
            matrix[slide] = {"status": "available" if n_ok == len(rows) else ("partial" if n_ok else "unavailable"), "inputs": rows}
        return {"slides": matrix, "missing": missing, "unverified": unverified}

    # ------------------------------------------------------------------ build
    def build(self) -> dict[str, Any]:
        anchors = self.anchors()
        bank_p = dt.date.fromisoformat(anchors["banking"]["period_end"]) if anchors["banking"]["period_end"] else None
        macro_p = dt.date.fromisoformat(anchors["macro"]["period_end"]) if anchors["macro"]["period_end"] else None
        cpi_p = dt.date.fromisoformat(anchors["prices"]["period_end"]) if anchors["prices"]["period_end"] else None
        fp: dict[str, Any] = {
            "report_type": "monthly", "as_of": self.as_of.isoformat(), "as_of_definition": "information cutoff at 23:59 Asia/Baku on the stated date",
            "information_set_mode": self.store.mode, "generated_at": utcnow(), "lang": self.lang,
            "edition": {"banking_period": bank_p.isoformat() if bank_p else None, "macro_period": macro_p.isoformat() if macro_p else None,
                        "cpi_period": cpi_p.isoformat() if cpi_p else None, "edition_month": bank_p.strftime("%Y-%m") if bank_p else None},
            "anchors": anchors,
        }
        slides: dict[str, Any] = {}
        bp, mp, cp = bank_p, macro_p, cpi_p
        # --- M03 scorecard
        rows = []
        for r in self.rcfg["scorecard"]:
            snap = self.snap(r["metric"], r.get("dims"), period=None, compare=r.get("change", "lag12"), key="scorecard." + r["key"])
            snap.update({"key": r["key"], "row_label": r["label"], "kind": r.get("kind", "pp"), "good": r.get("good", "neutral")})
            spark = self.chart_series(r["metric"], r.get("dims"), window=13)
            snap["sparkline"] = spark["points"]
            rows.append(snap)
        slides["M03"] = {"rows": rows}
        # --- M04 growth
        slides["M04"] = {
            "kpis": [self.snap("ssc.hl.gdp.growth", compare="prior_edition"), self.snap("ssc.hl.gdp_nonoil.growth", compare="prior_edition"),
                     self.snap("ssc.hl.gdp_oil.growth", compare="prior_edition"), self.snap("ssc.hl.gdp.level", compare="lag12"),
                     self.snap("ssc.gdp.nonoil_share_nominal", compare="lag12")],
            "chart_ytd_growth": [self.chart_series("ssc.hl.gdp.growth", label="GDP, real YTD growth"), self.chart_series("ssc.hl.gdp_nonoil.growth", label="Non-oil-gas GDP"),
                                 self.chart_series("ssc.hl.gdp_oil.growth", label="Oil-gas GDP")],
            "chart_quarterly": [self.chart_series("ssc.gdp.quarterly.total.real_yoy", window=16, label="Quarterly real GDP, y/y (2005 prices)")],
            "sector_table": [dict(self.snap(f"ssc.gdp.sector.{k}.real_growth_ytd", compare="prior_edition"), sector=k,
                                  share=self.snap(f"ssc.gdp.sector.{k}.share_nominal", compare="lag12") if self.eng.get(f"ssc.gdp.sector.{k}.share_nominal") else None)
                             for k in ["agriculture", "industry", "construction", "trade", "transport", "accommodation", "ict"]],
        }
        # --- M05 inflation & income
        slides["M05"] = {
            "kpis": [self.snap("ssc.cpi.all.yoy", compare="lag1"), self.snap("ssc.cpi.all.ytd", compare="lag1"), self.snap("ssc.cpi.all.mom", compare="lag1"),
                     self.snap("ssc.hl.wage.growth", compare="prior_edition"), self.snap("ssc.hl.wage.level", compare="lag12"),
                     self.snap("ssc.real_wage_growth_est", compare="prior_edition"), self.snap("ssc.hl.income.growth", compare="prior_edition"),
                     self.snap("ssc.hl.retail.growth", compare="prior_edition")],
            "chart_cpi": [self.chart_series("ssc.cpi.all.yoy", window=24, label="CPI y/y"), self.chart_series("ssc.cpi.food.yoy", window=24, label="Food y/y"),
                          self.chart_series("ssc.cpi.nonfood.yoy", window=24, label="Non-food y/y"), self.chart_series("ssc.cpi.services.yoy", window=24, label="Services y/y")],
            "chart_wage": [self.chart_series("ssc.hl.wage.growth", window=24, label="Nominal wage, YTD y/y"), self.chart_series("ssc.cpi.all.ytd", window=24, label="CPI, YTD average"),
                           self.chart_series("ssc.real_wage_growth_est", window=24, label="Real wage, estimate")],
        }
        # --- M06 sectors
        sect_keys = [("industry_nonoil", "Non-oil industry (output)"), ("agriculture", "Agriculture (output)"), ("transport", "Transport & storage (services)"),
                     ("ict", "ICT services"), ("retail", "Retail turnover"), ("investment", "Fixed investment"),
                     ("investment_nonoil", "Non-oil investment"), ("industry", "Industry incl. oil-gas (output)")]
        slides["M06"] = {"rows": [dict(self.snap(f"ssc.hl.{k}.growth", compare="prior_edition"), key=k, row_label=lbl, measure="real growth, YTD y/y") for k, lbl in sect_keys]
                         + [dict(self.snap("ssc.gdp.sector.construction.real_growth_ytd", compare="prior_edition"), key="construction", row_label="Construction (value added)", measure="real growth, YTD y/y")]}
        # --- M07 external
        exp_snap = self.snap("ssc.hl.exports.level", compare="lag12")
        imp_snap = self.snap("ssc.hl.imports.level", compare="lag12")
        exp_month = [dt.date.fromisoformat(exp_snap["latest"]["period"]).month] if exp_snap.get("latest") else None
        imp_month = [dt.date.fromisoformat(imp_snap["latest"]["period"]).month] if imp_snap.get("latest") else None
        slides["M07"] = {
            "kpis": [exp_snap, self.snap("ssc.hl.exports.growth", compare="prior_edition"),
                     imp_snap, self.snap("ssc.hl.imports.growth", compare="prior_edition"),
                     self.snap("ssc.hl.exports_nonoil.level", compare="lag12"), self.snap("ssc.hl.exports_nonoil.growth", compare="prior_edition"),
                     self.snap("ssc.hl.trade_balance.level", compare="lag12"),
                     self.snap("cba.reserves.official_usd", compare="lag12"), self.snap("cba.reserves.official_usd.yoy", compare="lag1"),
                     self.snap("ssc.hl.strategic_reserves.level", compare="lag12"), self.snap("cba.fx.usd_azn", compare="lag12")],
            "chart_trade": [self.chart_series("ssc.hl.exports.level", window=6, keep_months=exp_month, label="Exports, YTD"),
                            self.chart_series("ssc.hl.imports.level", window=6, keep_months=imp_month, label="Imports, YTD")],
            "chart_reserves": [self.chart_series("cba.reserves.official_usd", label="CBA official reserves, USD mln")],
        }
        # --- M08 lending
        contrib_keys = [("households", "Households"), ("trade_services", "Trade & services"), ("construction", "Construction"), ("transport_comm", "Transport & comm."),
                        ("industry", "Industry"), ("agriculture", "Agriculture"), ("mining_energy", "Mining & energy"), ("other_all", "Other sectors & instruments"),
                        ("overdue_unclassified", "Overdue (unclassified)")]
        slides["M08"] = {
            "kpis": [self.snap("cba.loans.total_ci", compare="lag12"), self.snap("cba.loans.total_ci.yoy", compare="lag1"), self.snap("cba.loans.total_ci.yoy_change", compare="lag12"),
                     self.snap("cba.loans.total_ci.mom_change", compare="lag1"), self.snap("cba.loans.banks.yoy", compare="lag1"), self.snap("cba.loans.nbci.yoy", compare="lag1"),
                     self.snap("cba.new_loans.total.3m", compare="lag12"), self.snap("cba.new_loans.total.yoy", compare="lag1"),
                     self.snap("cba.bank.portfolio.business.yoy", compare="lag1"), self.snap("cba.bank.portfolio.consumer.yoy", compare="lag1"), self.snap("cba.bank.portfolio.mortgage.yoy", compare="lag1")],
            "chart_stock": [self.chart_series("cba.loans.total_ci", label="Loans to the economy, AZN mln")],
            "chart_yoy": [self.chart_series("cba.loans.total_ci.yoy", label="y/y, %")],
            "contributions": [dict(self.snap(f"cba.loans.sector.{k}.contrib", compare="lag1"), key=k, row_label=lbl,
                                   yoy=self.snap(f"cba.loans.sector.{k}.yoy", compare="lag1") if self.eng.get(f"cba.loans.sector.{k}.yoy") else None,
                                   share=self.snap(f"cba.loans.sector.{k}.share", compare="lag12") if self.eng.get(f"cba.loans.sector.{k}.share") else None)
                              for k, lbl in contrib_keys],
            "total_yoy": self.snap("cba.loans.sector.real_total.yoy", compare="lag1"),
        }
        # --- M09 credit vs activity
        mapping = config.sources().get("sector_mapping", [])
        pts = []
        for mrow in mapping:
            credit = self.snap(mrow["cba_series"] + ".yoy", compare="lag1", key="m09." + mrow["key"] + ".credit")
            act = self.snap(mrow["ssc_metric"], compare="prior_edition", key="m09." + mrow["key"] + ".activity")
            share = self.snap(mrow["cba_series"] + ".share", compare="lag12", key="m09." + mrow["key"] + ".share")
            pts.append({"key": mrow["key"], "label": mrow["label_en"], "note": mrow.get("note"), "credit_yoy": credit, "activity_growth": act, "loan_share": share})
        slides["M09"] = {"points": pts}
        # --- M10 asset quality
        dec = self.eng.get("cba.bank.npl.decomp_yoy")
        decm = self.eng.get("cba.bank.npl.decomp_mom")

        def decomp(res: MetricResult | None) -> dict[str, Any] | None:
            if res is None or res.series.empty:
                return None
            d = res.series.index[-1]
            vals = {k: _f(v.get(d)) for k, v in res.extra.items()}
            vals.update({"period": d.isoformat(), "total_change_pp": _f(res.series[d])})
            for k, v in vals.items():
                if isinstance(v, float):
                    self.approved_numbers.append({"ref": f"{res.metric_id}.{k}", "value": v, "period": d.isoformat(), "unit": "pp" if "effect" in k or "change" in k else None})
            return vals
        slides["M10"] = {
            "kpis": [self.snap("cba.bank.npl.total", compare="lag12"), self.snap("cba.bank.npl.ratio", compare="lag12"), self.snap("cba.bank.npl.ratio", compare="lag1", key="cba.bank.npl.ratio.m"),
                     self.snap("cba.bank.npl.total.yoy", compare="lag1"), self.snap("cba.bank.npl.ratio_business", compare="lag12"), self.snap("cba.bank.npl.ratio_consumer", compare="lag12"),
                     self.snap("cba.bank.npl.ratio_mortgage", compare="lag12"), self.snap("cba.loans.overdue", compare="lag12"), self.snap("cba.loans.overdue_ratio", compare="lag12"),
                     self.snap("cba.bank.allowance_to_npl", compare="lag12"), self.snap("cba.bank.allowance_to_gross_loans", compare="lag12")],
            "decomposition_yoy": decomp(dec), "decomposition_mom": decomp(decm),
            "chart_npl": [self.chart_series("cba.bank.npl.total", label="NPL, AZN mln")], "chart_ratio": [self.chart_series("cba.bank.npl.ratio", label="NPL ratio, %"),
                                                                                                          self.chart_series("cba.loans.overdue_ratio", label="Overdue ratio (all CI), %")],
        }
        # --- M11 deposits
        slides["M11"] = {
            "kpis": [self.snap("cba.deposits.total", compare="lag12"), self.snap("cba.deposits.total.yoy", compare="lag1"), self.snap("cba.deposits.total.yoy_change", compare="lag12"),
                     self.snap("cba.deposits.total.mom_change", compare="lag1"), self.snap("cba.deposits.hh.total", compare="lag12"), self.snap("cba.deposits.hh.total.yoy", compare="lag1"),
                     self.snap("cba.deposits.nfc.total", compare="lag12"), self.snap("cba.deposits.nfc.total.yoy", compare="lag1"), self.snap("cba.deposits.fin.total.yoy", compare="lag1"),
                     self.snap("cba.deposits.hh.share", compare="lag12"), self.snap("cba.deposits.nfc.share", compare="lag12")],
            "contributions": [dict(self.snap(f"cba.deposits.{k}.contrib", compare="lag1"), key=k, row_label=lbl) for k, lbl in [("hh", "Households"), ("nfc", "Non-financial corporations"), ("fin", "Financial corporations")]],
            "chart_stock": [self.chart_series("cba.deposits.hh.total", label="Households"), self.chart_series("cba.deposits.nfc.total", label="Non-financial corporations"),
                            self.chart_series("cba.deposits.fin.total", label="Financial corporations")],
            "chart_yoy": [self.chart_series("cba.deposits.total.yoy", label="Total deposits y/y"), self.chart_series("cba.deposits.hh.total.yoy", label="Household y/y"),
                          self.chart_series("cba.deposits.nfc.total.yoy", label="Non-financial corporate y/y")],
        }
        # --- M12 dollarisation
        slides["M12"] = {
            "kpis": [self.snap("cba.deposits.fx_share", compare="lag12"), self.snap("cba.deposits.fx_share.pp_mom", compare="lag1"), self.snap("cba.deposits.hh.fx_share", compare="lag12"),
                     self.snap("cba.deposits.nfc.fx_share", compare="lag12"), self.snap("cba.loans.fx_share", compare="lag12"), self.snap("cba.deposits.time_share", compare="lag12"),
                     self.snap("cba.deposits.hh.time_share", compare="lag12"), self.snap("cba.hh_savings.fx_share", compare="lag12"), self.snap("cba.bank.fx_assets_share", compare="lag12"),
                     self.snap("cba.bank.fx_liab_share", compare="lag12")],
            "chart_fx": [self.chart_series("cba.deposits.fx_share", label="Deposits FX share"), self.chart_series("cba.deposits.hh.fx_share", label="Household deposits FX share"),
                         self.chart_series("cba.loans.fx_share", label="Loans FX share")],
            "chart_term": [self.chart_series("cba.deposits.time_share", label="Time-deposit share, total"), self.chart_series("cba.deposits.hh.time_share", label="Time share, households")],
        }
        # --- M13 rates
        slides["M13"] = {
            "kpis": [self.snap("cba.rates.new.loan", {"currency": "AZN"}, compare="lag12"), self.snap("cba.rates.new.deposit", {"currency": "AZN"}, compare="lag12"),
                     self.snap("cba.rates.new.spread", {"currency": "AZN"}, compare="lag12"), self.snap("cba.rates.new.loan", {"currency": "FX"}, compare="lag12"),
                     self.snap("cba.rates.new.deposit", {"currency": "FX"}, compare="lag12"), self.snap("cba.rates.new.spread", {"currency": "FX"}, compare="lag12"),
                     self.snap("cba.rates.out.loan", {"currency": "AZN"}, compare="lag12"), self.snap("cba.rates.out.deposit", {"currency": "AZN"}, compare="lag12"),
                     self.snap("cba.rates.new.spread", {"currency": "AZN"}, compare="lag1", key="cba.rates.new.spread.azn.m")],
            "chart_azn": [self.chart_series("cba.rates.new.loan", {"currency": "AZN"}, label="New loans, AZN"), self.chart_series("cba.rates.new.deposit", {"currency": "AZN"}, label="New term deposits, AZN"),
                          self.chart_series("cba.rates.new.spread", {"currency": "AZN"}, label="Spread, pp")],
            "chart_fx": [self.chart_series("cba.rates.new.loan", {"currency": "FX"}, label="New loans, FX"), self.chart_series("cba.rates.new.deposit", {"currency": "FX"}, label="New term deposits, FX"),
                         self.chart_series("cba.rates.new.spread", {"currency": "FX"}, label="Spread, pp")],
        }
        # --- M14 credit vs funding
        slides["M14"] = {
            "kpis": [self.snap("cba.loans.total_ci.yoy", compare="lag1", key="m14.loans_yoy"), self.snap("cba.deposits.total.yoy", compare="lag1", key="m14.deposits_yoy"),
                     self.snap("cba.loans_minus_deposits.yoy_growth_gap", compare="lag12"), self.snap("cba.ldr", compare="lag12"), self.snap("cba.ldr.pp_yoy", compare="lag1"),
                     self.snap("cba.loans.total_ci.yoy_change", compare="lag12", key="m14.loans_change"), self.snap("cba.deposits.total.yoy_change", compare="lag12", key="m14.deposits_change"),
                     self.snap("cba.loans.long_share", compare="lag12"), self.snap("cba.loans.short_share", compare="lag12"), self.snap("cba.deposits.time_share", compare="lag12", key="m14.time_share"),
                     self.snap("cba.credit_to_gdp", compare="lag12"), self.snap("cba.money.m3.yoy", compare="lag1")],
            "chart_growth": [self.chart_series("cba.loans.total_ci.yoy", label="Loans y/y"), self.chart_series("cba.deposits.total.yoy", label="Deposits y/y")],
            "chart_ldr": [self.chart_series("cba.ldr", label="Loan-to-deposit ratio, %")],
            "chart_maturity": [self.chart_series("cba.loans.long_share", label="Long-term share of loans"), self.chart_series("cba.deposits.time_share", label="Time share of deposits")],
        }
        # --- M15 profitability
        bridge_items = [("net_interest_income", "Net interest income", 1), ("non_interest_income", "Non-interest income", 1), ("non_interest_expense", "Operating expense", -1),
                        ("provisions", "Provision charge", -1), ("other_income", "Other income", 1), ("tax", "Profit tax", -1)]
        bridge = []
        for k, lbl, sign in bridge_items:
            s = self.snap(f"cba.bank.pnl.{k}.yoy_change", compare="lag12", key=f"bridge.{k}")
            bridge.append({"key": k, "label": lbl, "sign": sign, "change": s["latest"]["value"] if s["available"] else None,
                           "contribution": (sign * s["latest"]["value"]) if s["available"] and s["latest"]["value"] is not None else None, "period": s["latest"]["period"] if s["available"] else None})
        np_snap = self.snap("cba.bank.pnl.net_profit", compare="lag12")
        resid = None
        if np_snap["available"] and np_snap["change"] is not None and all(b["contribution"] is not None for b in bridge):
            resid = np_snap["change"] - sum(b["contribution"] for b in bridge)
        slides["M15"] = {
            "kpis": [np_snap, self.snap("cba.bank.pnl.net_profit.yoy", compare="lag1"), self.snap("cba.bank.pnl.net_profit.monthly", compare="lag12"),
                     self.snap("cba.bank.pnl.net_interest_income", compare="lag12"), self.snap("cba.bank.pnl.net_interest_income.yoy", compare="lag1"),
                     self.snap("cba.bank.pnl.non_interest_income", compare="lag12"), self.snap("cba.bank.pnl.non_interest_expense", compare="lag12"),
                     self.snap("cba.bank.pnl.provisions", compare="lag12"), self.snap("cba.bank.pnl.cost_to_income", compare="lag12"),
                     self.snap("cba.bank.roa_annualised", compare="lag12"), self.snap("cba.bank.roe_annualised", compare="lag12"), self.snap("cba.bank.pnl.provisions_to_income", compare="lag12")],
            "bridge": {"items": bridge, "start": np_snap["prior"], "end": np_snap["latest"], "residual": resid},
            "chart_profit": [self.chart_series("cba.bank.pnl.net_profit.monthly", label="Net profit, monthly (derived from YTD)")],
            "chart_ytd": [self.chart_series("cba.bank.pnl.net_profit", window=24, label="Net profit, YTD")],
        }
        # --- M16 capital & liquidity
        slides["M16"] = {
            "kpis": [self.snap("cba.bank.capital.total", {"currency": "all"}, compare="lag12"), self.snap("cba.bank.capital.total.yoy", compare="lag1"),
                     self.snap("cba.bank.equity_to_assets", compare="lag12"), self.snap("cba.bank.assets.total", {"currency": "all"}, compare="lag12"),
                     self.snap("cba.bank.assets.total.yoy", compare="lag1"), self.snap("cba.bank.liquid_assets", compare="lag12"), self.snap("cba.bank.liquid_assets_ratio", compare="lag12"),
                     self.snap("cba.bank.fx_assets_share", compare="lag12", key="m16.fx_assets"), self.snap("cba.bank.fx_liab_share", compare="lag12", key="m16.fx_liab"),
                     self.snap("cba.bank.deposits_nonfi.yoy", compare="lag1"), self.snap("cba.bank.count", compare="lag12")],
            "chart_capital": [self.chart_series("cba.bank.capital.total", {"currency": "all"}, label="Total capital (book), AZN mln")],
            "chart_ratios": [self.chart_series("cba.bank.equity_to_assets", label="Capital / assets, %"), self.chart_series("cba.bank.liquid_assets_ratio", label="Liquid assets / assets, %")],
        }
        # --- M17 regions
        reg_rows = []
        for dims in self.store.dims_for("cba.loans.region.total"):
            region = dims.get("region")
            if not region or region == "national":
                continue
            l = self.store.observation("cba.loans.region.total", bp, dims) if bp else None
            d = self.store.observation("cba.hh_savings.region.total", bp, dims) if bp else None
            reg_rows.append({"region": region, "loans": _f(l["value"]) if l else None, "deposits": _f(d["value"]) if d else None})
        nat_l = self.store.observation("cba.loans.region.total", bp, {"region": "national"}) if bp else None
        nat_d = self.store.observation("cba.hh_savings.region.total", bp, {"region": "national"}) if bp else None
        for r in reg_rows:
            r["loan_share"] = (r["loans"] / nat_l["value"] * 100.0) if (r["loans"] is not None and nat_l and nat_l["value"]) else None
            r["deposit_share"] = (r["deposits"] / nat_d["value"] * 100.0) if (r["deposits"] is not None and nat_d and nat_d["value"]) else None
            r["ldr"] = (r["loans"] / r["deposits"] * 100.0) if (r["loans"] and r["deposits"]) else None
            for k in ("loans", "deposits", "loan_share", "deposit_share", "ldr"):
                if r[k] is not None:
                    self.approved_numbers.append({"ref": f"region.{r['region']}.{k}", "value": r[k], "period": bp.isoformat() if bp else None, "unit": "AZN mln" if k in ("loans", "deposits") else "%"})
        reg_rows.sort(key=lambda r: -(r["loans"] or 0))
        self._register_inputs("cba.loans.region.total", {"region": "national"})
        self._register_inputs("cba.hh_savings.region.total", {"region": "national"})
        slides["M17"] = {"rows": reg_rows, "national": {"loans": _f(nat_l["value"]) if nat_l else None, "deposits": _f(nat_d["value"]) if nat_d else None, "period": bp.isoformat() if bp else None},
                         "note": "Loans: banks only, by booking region (CBA table 2.10). Deposits: household savings by region (CBA table 2.14). Baku includes head-office bookings."}
        if not any(r["loans"] is not None for r in reg_rows):
            held = self.store.latest_any("cba.loans.region.total")
            slides["M17"]["unavailable"] = (
                f"The CBA regional tables carry a single month, and the month held ({held[0].isoformat() if held else 'none'}) "
                f"is not this edition's banking month ({bp.isoformat() if bp else 'unknown'}). No regional figures are shown "
                f"rather than figures for a different month.")
        # --- M18 next releases (expected dates from CBA schedule lags)
        nxt = []
        for sid, scfg, ds in config.iter_datasets():
            if ds.get("role") not in ("anchor", "companion"):
                continue
            st = self.states.get(ds["id"], {})
            latest = st.get("latest_period_end")
            lag = ds.get("expected_lag_days")
            if latest and lag:
                nxt_period = shift_months(dt.date.fromisoformat(latest), 1)
                expected = nxt_period + dt.timedelta(days=int(lag))
                overdue = expected < self.as_of
                nxt.append({"dataset_id": ds["id"], "title": ds.get("title_en"), "source": sid, "next_period": nxt_period.isoformat(), "expected_by": expected.isoformat(),
                            "status": ("past expected date, not yet observed" if overdue else "expected (schedule), not confirmed"),
                            "basis": "official schedule: within %d days after the reporting period (expected, not confirmed)" % lag})
        # an announced date beats a cadence estimate; a publication with neither says so
        for entry in self.publication_releases():
            nxt.append(entry)
        nxt.sort(key=lambda r: (r["expected_by"] is None, (r["expected_by"] or "") < self.as_of.isoformat(), r["expected_by"] or "z"))
        slides["M18"] = {"next_releases": nxt[:12], "implications": self.management_implications()}
        # --- policy and stability (M19-M22) and their appendices
        pubs = self.publication_facts()
        fp["publications"] = pubs
        slides.update(self.policy_slides(pubs))
        slides.update(self.stability_slides(pubs))
        # --- appendices
        slides["A02"] = {"register": self.source_register(), "freshness": self.freshness()}
        slides["A05"] = {"decisions": pubs["policy"]["rate_path"], "forecasts": pubs["policy"]["forecasts"],
                         "stress": pubs["stability"]["stress_tests"], "reconciliation": pubs["stability"]["source_reconciliation"]}
        slides["A06"] = {"publications": pubs["register"], "archive_gaps": pubs["archive_gaps"],
                         "cutoff_note": pubs["cutoff_note"], "cutoff_limitations": pubs.get("cutoff_limitations"),
                         "held_for_review": pubs.get("held_for_review") or []}
        slides["A03"] = {"revisions": self.revisions_since_last_edition()}
        slides["A04"] = {"series": [self.chart_series(s, d, window=self.window) for s, d in [("cba.loans.total_ci", None), ("cba.deposits.total", None), ("cba.deposits.hh.total", None),
                                                                                              ("cba.bank.npl.ratio", None), ("cba.rates.new.loan", {"currency": "AZN"}), ("cba.reserves.official_usd", None)]]}
        fp["slides"] = slides
        fp["availability"] = self.availability_matrix()
        fp["metrics"] = self.metric_refs
        fp["approved_numbers"] = self.approved_numbers
        fp["sources"] = [self._doc_summary(d) for d in sorted(self.sources_used.values(), key=lambda r: (r["source_id"], r["dataset_id"], r["retrieved_at"]))]
        fp["definitions"] = self.definitions()
        fp["flags"] = self.monitoring_flags()
        # a failure inside the window this edition displays is critical; older history is a warning
        window_start = shift_months(bank_p, -self.window) if bank_p else None
        q = validate_all(self.db, write=False, display_from=window_start)
        fp["quality"] = {"summary": q["summary"], "checks": [{k: v for k, v in c.items() if k != "datasets"} for c in q["checks"]]}
        fp["fact_pack_hash"] = hashlib.sha256(
            json.dumps({k: fp[k] for k in ("slides", "metrics", "publications")}, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return fp

    # ------------------------------------------------- policy and stability facts
    def publication_facts(self) -> dict[str, Any]:
        """Policy and stability facts, and the claim-able snapshots that go with them."""
        if getattr(self, "_pubs", None) is None:
            from .publications.factpack import PublicationFacts

            self._pubs = PublicationFacts(self.db, self.as_of, self.lang, metrics=self.metric_refs).build()
        return self._pubs

    def policy_slides(self, pubs: dict[str, Any]) -> dict[str, Any]:
        """M19 policy stance and M20 inflation outlook.

        The stance sentence describes the rate decision only. Where the rate did not move but the
        Central Bank's reasoning did, that is what the slide says, because an unchanged rate with a
        changed outlook is a different signal from an unchanged rate with an unchanged outlook.
        """
        pol = pubs["policy"]
        decision, previous = pol["decision"], pol["previous_decision"]
        rate = self.snap("cba.policy.rate", None, key="cba.policy.rate")
        floor = self.snap("cba.policy.corridor_floor", None, key="cba.policy.corridor_floor")
        ceiling = self.snap("cba.policy.corridor_ceiling", None, key="cba.policy.corridor_ceiling")
        cpi = self.snap("ssc.cpi.all.yoy", None, key="ssc.cpi.all.yoy")
        forecasts = []
        for f in pol["forecasts"]["current"]:
            key = f"{f['series_id']}|{json.dumps(f['dims'], sort_keys=True)}" if f.get("dims") else f["series_id"]
            snap = self.snap(f["series_id"], f.get("dims"), key=key)
            forecasts.append({**f, "snapshot": snap, "claim_ref": key})
        m19 = {
            "decision": decision, "previous_decision": previous, "stance": pol["stance"],
            "corridor_now": pol["corridor_now"], "rate_path": pol["rate_path"], "corridor_path": pol["corridor_path"],
            "rate": rate, "floor": floor, "ceiling": ceiling,
            "review": pol["review"], "next_decision": pol["next_decision"],
            "comparison": self._stance_comparison_table(pol),
            "note": "Rate levels come from the decision table printed in the Monetary Policy Review; the rationale "
                    "comes from the decision statement. Effective dates are shown only when the statement gives one.",
        }
        m20 = {
            "cpi": cpi, "forecasts": forecasts, "revisions": pol["forecasts"]["revisions"],
            "current_vintage": pol["forecasts"]["current_vintage"], "previous_vintage": pol["forecasts"]["previous_vintage"],
            "target": {"statement": "The Central Bank publishes an inflation target range; the value shown is the "
                                    "Bank's own published projection, not our estimate."},
            "note": pol["forecasts"]["note"],
            "risks": self._risk_passages(pol),
        }
        return {"M19": m19, "M20": m20}

    def _stance_comparison_table(self, pol: dict[str, Any]) -> list[dict[str, Any]]:
        """Previous assessment -> current assessment -> evidence -> banking implication."""
        cur, prev = pol["decision"], pol["previous_decision"]
        if not cur:
            return []
        prev_only, cur_only = _rationale_difference((prev or {}).get("rationale"), cur.get("rationale"))
        rows = [{
            "dimension": "Policy rate",
            "previous": f"{prev['policy_rate']}% on {prev['announcement_date']}" if prev and prev.get("policy_rate") is not None else "not established",
            "current": f"{cur['policy_rate']}% on {cur['announcement_date']}" if cur.get("policy_rate") is not None else "not established",
            "evidence": "decision table in the Monetary Policy Review",
            "implication": "funding cost floor for manat liquidity operations",
        }, {
            "dimension": "Corridor width",
            "previous": (f"{round(prev['corridor_ceiling'] - prev['corridor_floor'], 2)} pp"
                         if prev and prev.get("corridor_ceiling") is not None and prev.get("corridor_floor") is not None else "not established"),
            "current": (f"{pol['corridor_now']['width_pp']} pp" if pol["corridor_now"]["width_pp"] is not None else "not established"),
            "evidence": "corridor floor and ceiling as decided",
            "implication": "range within which overnight money-market rates can move",
        }, {
            "dimension": "Stated rationale (only what differs)",
            "previous": prev_only,
            "current": cur_only,
            "evidence": f"decision statement, {cur.get('rationale_language') or 'az'} edition",
            "implication": "direction of travel for deposit and lending rates over the next quarter",
        }]
        return rows

    def _risk_passages(self, pol: dict[str, Any]) -> list[dict[str, Any]]:
        """Risks the Central Bank itself names, quoted from the decision statement.

        Each one carries the passage it came from so the narrative can cite it and the validator can
        confirm the quotation against the stored text.
        """
        cur = pol.get("decision") or {}
        pub_id = ((cur.get("publication") or {}).get("publication_id"))
        if not pub_id:
            return []
        rows = self.db.passages(publication_id=pub_id, language="en") or self.db.passages(publication_id=pub_id)
        out = []
        for r in rows:
            for sentence in re.split(r"(?<=[.!?])\s+", r["text"] or ""):
                if len(sentence) > 45 and re.search(r"risk|uncertain|upside|downside", sentence, re.IGNORECASE):
                    out.append({"text": sentence.strip()[:320], "source": "CBA decision statement",
                                "date": cur.get("announcement_date"), "classification": "cba_assessment",
                                "passage_id": r["passage_id"], "language": r["language"]})
        return out[:5]

    def stability_slides(self, pubs: dict[str, Any]) -> dict[str, Any]:
        """M21 stability dashboard and M22 systemic vulnerabilities.

        Every row carries its own reporting date and the date its report was published, because
        these measures are half-yearly and are older than the banking month of the deck.
        """
        stab = pubs["stability"]
        dashboard = []
        for row in stab["dashboard"]:
            key = row["series_id"] + (("|" + json.dumps(row["dims"], sort_keys=True)) if row["dims"] else "")
            snap = self.snap(row["series_id"], row["dims"] or None, key=key)
            dashboard.append({**row, "snapshot": snap, "claim_ref": key})
        # only the most recent exercise is shown; earlier rounds stay in the appendix, since results
        # from two different exercises are not a time series
        exercises = sorted({(r.get("dims") or {}).get("exercise") for r in stab["stress_tests"]["results"] if (r.get("dims") or {}).get("exercise")})
        latest_exercise = exercises[-1] if exercises else None
        stress = []
        for row in stab["stress_tests"]["results"]:
            if latest_exercise and (row.get("dims") or {}).get("exercise") != latest_exercise:
                continue
            key = row["series_id"] + (("|" + json.dumps(row["dims"], sort_keys=True)) if row["dims"] else "")
            stress.append({**row, "snapshot": self.snap(row["series_id"], row["dims"] or None, key=key), "claim_ref": key})
        book = {
            "equity_to_assets": self.snap("cba.bank.equity_to_assets", None, key="cba.bank.equity_to_assets"),
            "liquid_assets_ratio": self.snap("cba.bank.liquid_assets_ratio", None, key="cba.bank.liquid_assets_ratio"),
        }
        m21 = {
            "report": stab["report"], "previous_report": stab["previous_report"], "dashboard": dashboard,
            "book_measures": book, "reconciliation": stab["source_reconciliation"], "as_of_note": stab["as_of_note"],
            "distinction": "Regulatory capital adequacy and the liquidity coverage ratio come from the Financial "
                           "Stability Report. Capital-to-assets and liquid-assets-to-assets are book ratios from the "
                           "monthly balance sheet. They measure different things and are not comparable.",
        }
        m22 = {
            "stress": stress, "scenarios": stab["stress_tests"].get("note"),
            "concentration": self._concentration_facts(),
            "funding": {"ldr": self.snap("cba.ldr", None, key="cba.ldr"),
                        "deposit_fx_share": self.snap("cba.deposits.fx_share", None, key="cba.deposits.fx_share"),
                        "corporate_deposits_yoy": self.snap("cba.deposits.nfc.total.yoy", None, key="cba.deposits.nfc.total.yoy")},
            "exercise": latest_exercise,
            "note": "Stress-test results are projections under the Central Bank's published scenarios from the "
                    "exercise date shown. They are not forecasts and not outcomes.",
        }
        return {"M21": m21, "M22": m22}

    def _concentration_facts(self) -> list[dict[str, Any]]:
        """Published concentrations a board would ask about: region, sector and product."""
        out = []
        for ref, dims, label in (("cba.loans.sector.households.share", None, "Household share of real-sector loans"),
                                 ("cba.loans.sector.trade_services.share", None, "Trade and services share of real-sector loans"),
                                 ("cba.loans.sector.construction.share", None, "Construction share of real-sector loans"),
                                 ("cba.deposits.time_share", None, "Time deposits as a share of total deposits")):
            snap = self.snap(ref, dims, key=ref)
            if snap.get("available"):
                out.append({"label": label, "claim_ref": ref, "snapshot": snap})
        return out

    def publication_releases(self) -> list[dict[str, Any]]:
        """Next release entries for the narrative publications.

        An announced date is used as announced. Where none is announced, the entry says so; a
        cadence-based date is only ever shown when it is labelled as an estimate.
        """
        pubs = self.publication_facts()
        out: list[dict[str, Any]] = []
        nd = pubs["policy"]["next_decision"]
        out.append({"dataset_id": "cba_policy_decisions", "title": "Monetary policy decision", "source": "CBA_POLICY_DECISIONS",
                    "next_period": nd.get("date"), "expected_by": nd.get("date"),
                    "status": "announced by the Central Bank" if nd.get("status") == "announced" else "not announced",
                    "basis": nd.get("basis")})
        for pub_type, title, cadence_months in (("monetary_policy_review", "Monetary Policy Review", 3),
                                                ("financial_stability_report", "Financial Stability Report", 6)):
            latest = (pubs["policy"]["review"] if pub_type == "monetary_policy_review" else pubs["stability"]["report"])
            if not latest:
                continue
            published = latest.get("published_at")
            estimate = None
            if published:
                base = dt.date.fromisoformat(published)
                estimate = shift_months(base, cadence_months).isoformat()
            out.append({"dataset_id": pub_type, "title": title, "source": "CBA",
                        "next_period": None, "expected_by": estimate,
                        "status": "estimate from the publication cadence, not announced",
                        "basis": f"previous edition {latest.get('edition')} released {published or 'date unknown'}; "
                                 f"the Central Bank does not publish a calendar for this report"})
        return out

    def management_implications(self) -> list[dict[str, Any]]:
        """Evidence -> transmission channel -> monitoring indicator -> question.

        These are conditional analytical implications of public evidence. None of them asserts
        anything about this bank's own exposure, which is not in any of these sources.
        """
        pubs = self.publication_facts()
        pol, stab = pubs["policy"], pubs["stability"]
        rows: list[dict[str, Any]] = []
        decision = pol.get("decision") or {}
        if decision.get("policy_rate") is not None:
            rows.append({
                "evidence": pol["stance"].get("statement"),
                "channel": "Policy rate and corridor set the floor under manat funding costs and the return on "
                           "placements with the Central Bank",
                "monitor": "new-business deposit and loan rates (CBA table 3.2.1), interbank rates, own cost of funds",
                "question": "If the corridor stays where it is into 2027, what happens to our deposit repricing "
                            "schedule and to the margin on fixed-rate lending written this year?",
                "function": "Treasury / ALM", "classification": "interpretation",
            })
        inflation = [f for f in pol["forecasts"]["current"] if f["series_id"] == "cba.forecast.inflation"]
        if inflation:
            rows.append({
                "evidence": f"The Central Bank's {pol['forecasts']['current_vintage']} round projects inflation of "
                            f"{inflation[0]['value']}% for {inflation[0].get('horizon')}",
                "channel": "Inflation shapes nominal income growth, deposit demand and real borrowing costs",
                "monitor": "CPI releases, the next projection round, deposit growth by segment",
                "question": "Which assumptions in our own planning differ from the Central Bank's published "
                            "projection, and what would make us revise them?",
                "function": "Finance / Strategy", "classification": "interpretation",
            })
        car = next((d for d in stab["dashboard"] if d["series_id"] == "cba.fsr.car"), None)
        if car:
            rows.append({
                "evidence": f"Sector regulatory capital adequacy was {car['value']}% at {car['observation_date']} "
                            f"({car['publication']['label']}, published {car['publication']['published_at']})",
                "channel": "System capital headroom conditions supervisory tolerance and peer pricing of risk",
                "monitor": "our own capital adequacy against the requirement, and the next stability report",
                "question": "How does our capital headroom compare with the sector figure, and what would the "
                            "published adverse scenario do to it?",
                "function": "CRO / Capital management", "classification": "management_question",
            })
        return rows

    # ------------------------------------------------------------------ registers
    def _doc_summary(self, d: dict[str, Any]) -> dict[str, Any]:
        return {k: d.get(k) for k in ("doc_id", "source_id", "dataset_id", "title_original", "title_en", "document_url", "discovery_url", "published_at",
                                       "published_at_basis", "retrieved_at", "sha256", "content_type", "size_bytes", "status")}

    def _latest_publication_period(self, dataset_id: str) -> str | None:
        """Latest period covered by a dataset that yields publications rather than observations.

        The decision releases and the policy directions carry passages and decision records, not a
        numeric series, so their freshness is the latest edition they report on.
        """
        row = self.db.conn.execute(
            "SELECT MAX(COALESCE(p.reporting_period_end, p.announcement_date, p.published_at)) "
            "FROM publications p JOIN publication_documents pd USING (publication_id) "
            "JOIN documents d ON d.doc_id = pd.doc_id WHERE d.dataset_id = ?", (dataset_id,)).fetchone()
        return row[0] if row and row[0] else None

    def _latest_publication_release(self, dataset_id: str) -> tuple[str | None, str | None]:
        """Release date of the newest edition of a publication dataset, with the basis for it."""
        row = self.db.conn.execute(
            "SELECT p.published_at, p.published_at_basis FROM publications p "
            "JOIN publication_documents pd USING (publication_id) JOIN documents d ON d.doc_id = pd.doc_id "
            "WHERE d.dataset_id = ? AND p.published_at IS NOT NULL ORDER BY p.published_at DESC LIMIT 1",
            (dataset_id,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def source_register(self) -> list[dict[str, Any]]:
        out = []
        for sid, scfg, ds in config.iter_datasets():
            st = self.states.get(ds["id"], {})
            docs = [r for r in self.db.documents_for_dataset(ds["id"])]
            latest_doc = docs[-1] if docs else None
            pub_release, pub_basis = self._latest_publication_release(ds["id"])
            doc_release = latest_doc["published_at"] if latest_doc else None
            doc_basis = latest_doc["published_at_basis"] if latest_doc else None
            # documents are not ordered by date, so the newest edition of a publication dataset can
            # be more recent than the document row that happens to be last
            if pub_release and (not doc_release or pub_release > doc_release):
                doc_release, doc_basis = pub_release, pub_basis
            out.append({"source_id": sid, "institution": scfg.get("institution"), "dataset_id": ds["id"], "title_en": ds.get("title_en"), "role": ds.get("role"),
                        "discovery_url": scfg.get("entry_point"), "document_url": latest_doc["document_url"] if latest_doc else None,
                        "title_original": latest_doc["title_original"] if latest_doc else None, "format": (latest_doc["stored_path"] or "").rsplit(".", 1)[-1] if latest_doc else None,
                        "published_at": doc_release, "published_at_basis": doc_basis,
                        "frequency": ds.get("frequency"), "expected_lag_days": ds.get("expected_lag_days"), "parser": ds.get("parser"), "status": st.get("status"),
                        "latest_period_end": st.get("latest_period_end") or self._latest_publication_period(ds["id"]),
                        "n_documents": len(docs), "history_in_file": ds.get("history_in_file"),
                        "known_limitations": ds.get("limitations")})
        return out

    def freshness(self) -> list[dict[str, Any]]:
        today = self.as_of
        out = []
        for sid, scfg, ds in config.iter_datasets():
            st = self.states.get(ds["id"], {})
            latest = st.get("latest_period_end")
            age = (today - dt.date.fromisoformat(latest)).days if latest else None
            out.append({"dataset_id": ds["id"], "latest_period_end": latest, "age_days": age, "expected_lag_days": ds.get("expected_lag_days"), "status": st.get("status")})
        return out

    def revisions_since_last_edition(self) -> list[dict[str, Any]]:
        eds = self.db.editions("monthly")
        since = eds[-1]["generated_at"] if eds else "1970-01-01"
        out = []
        for v in self.db.vintages_since(since):
            if v["n_revisions"]:
                out.append({"vintage_id": v["vintage_id"], "dataset_id": v["dataset_id"], "created_at": v["created_at"], "n_revisions": v["n_revisions"],
                            "examples": json.loads(v["revision_summary"] or "[]")[:5]})
        return out

    def definitions(self) -> list[dict[str, Any]]:
        out = []
        for m in self.eng.cfg:
            out.append({"id": m["id"], "label": m.get("label_en"), "formula": m["formula"], "unit": m.get("unit"), "basis": m.get("basis"),
                        "inputs": [m.get(k) for k in ("input", "numerator", "denominator", "numerators", "denominators", "component", "total", "a", "b", "nominal", "price", "inputs", "components") if m.get(k)]})
        return out

    def monitoring_flags(self) -> list[dict[str, Any]]:
        out = []
        for f in self.settings.get("monitoring_flags", []):
            s = self._series(f["metric"])
            if s.empty or len(s) < int(f.get("min_obs", 13)):
                out.append({"id": f["id"], "status": "insufficient_observations"})
                continue
            w = int(f.get("window", 12))
            cur = float(s.iloc[-1])
            prev = float(s.iloc[-1 - w]) if len(s) > w else None
            if prev is None:
                out.append({"id": f["id"], "status": "insufficient_observations"})
                continue
            move = cur - prev
            thr = float(f.get("threshold_pp", 1.0))
            hit = (f.get("direction") == "up" and move >= thr) or (f.get("direction") == "down" and move <= -thr) or (f.get("direction") == "any" and abs(move) >= thr)
            out.append({"id": f["id"], "metric": f["metric"], "window": w, "direction": f.get("direction"), "threshold_pp": thr, "current": cur, "comparison": prev,
                        "move": move, "period": s.index[-1].isoformat(), "triggered": bool(hit), "note": "prompt for a source check and investigation, not a risk score"})
        return out


def build_fact_pack(report_type: str = "monthly", as_of: str | None = None, lang: str | None = None, db: Database | None = None, write: bool = True,
                    historical: bool = False) -> tuple[dict[str, Any], Path | None]:
    paths = config.paths()
    paths.ensure()
    lang = lang or config.settings().get("language", "en")
    own = db is None
    db = db or Database(paths.db_path)
    as_of_d = parse_as_of(as_of)
    b = FactPackBuilder(db, as_of_d, lang=lang, historical=historical)
    fp = b.build()
    fp["report_type"] = report_type
    path = None
    if write:
        d = paths.state_dir / "fact_packs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"fact_pack_{report_type}_{as_of_d.isoformat()}.json"
        path.write_text(json.dumps(fp, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if own:
        db.close()
    return fp, path
