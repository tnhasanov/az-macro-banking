"""Deterministic metric engine driven by config/metrics.yaml.

Every metric is a full time series computed from stored (unrounded) observations.
Incompatible period types are rejected at configuration time (see `_check_types`).
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .. import config
from ..util.periods import shift_months
from .data import ObservationStore

STOCK_TYPES = {"month_end_stock", "month_end_average_rate"}
FLOW_TYPES = {"monthly_flow", "monthly_average_rate"}


class MetricConfigError(Exception):
    pass


@dataclass
class MetricResult:
    metric_id: str
    dims: dict[str, str]
    series: pd.Series
    unit: str | None
    label_en: str | None
    formula: str
    inputs: list[dict[str, Any]]
    period_type: str | None
    basis: str | None = None
    flags: dict[str, list[str]] = field(default_factory=dict)   # period -> flags (e.g. negative base)
    extra: dict[str, pd.Series] = field(default_factory=dict)   # decomposition components


class MetricEngine:
    def __init__(self, store: ObservationStore, cutoff: dt.date | None = None):
        self.store = store
        self.cutoff = cutoff
        self.cfg = config.metrics_config().get("metrics", [])
        self.results: dict[tuple[str, str], MetricResult] = {}
        self._by_id = {m["id"]: m for m in self.cfg}

    # ------------------------------------------------------------------ inputs
    def _get(self, sid: str, dims: dict[str, str] | None = None) -> tuple[pd.Series, str | None, dict[str, Any]]:
        """Fetch an observation series or a previously computed metric."""
        dims = dims or {}
        if sid in self._by_id and not dims and self._by_id[sid].get("dims"):
            dims = dict(self._by_id[sid]["dims"])   # a metric defined on a dimension slice
        key = (sid, json.dumps(dims, sort_keys=True))
        if key in self.results:
            r = self.results[key]
            return r.series, r.period_type, {"metric_id": sid, "dims": dims}
        if sid in self._by_id and key not in self.results:
            self.compute(sid, dims)
            if key in self.results:
                r = self.results[key]
                return r.series, r.period_type, {"metric_id": sid, "dims": dims}
        s = self.store.series(sid, dims, cutoff=self.cutoff)
        info = self.store.info(sid, dims)
        ptype = info.period_type if info else None
        return s, ptype, {"series_id": sid, "dims": dims, "period_type": ptype, "unit": info.unit if info else None,
                          "population": info.population if info else None, "doc_ids": info.doc_ids if info else []}

    @staticmethod
    def _check_types(mid: str, types: list[str | None], allowed: list[str] | None) -> None:
        clean = [t for t in types if t]
        if allowed:
            bad = [t for t in clean if t not in allowed]
            if bad:
                raise MetricConfigError(f"{mid}: period types {bad} not in allowed {allowed}")
            return
        if len(set(clean)) > 1:
            # stock vs flow or ytd mixes are rejected unless explicitly allowed
            raise MetricConfigError(f"{mid}: incompatible period types combined: {sorted(set(clean))}")

    # ------------------------------------------------------------------ compute
    def compute_all(self) -> dict[tuple[str, str], MetricResult]:
        errors: dict[str, str] = {}
        for m in self.cfg:
            try:
                self.compute(m["id"])
            except MetricConfigError as exc:
                errors[m["id"]] = str(exc)
        self.errors = errors
        return self.results

    def compute(self, mid: str, dims: dict[str, str] | None = None) -> None:
        m = self._by_id[mid]
        f = m["formula"]
        dims = dims if dims is not None else dict(m.get("dims", {}))
        by_dims = m.get("by_dims")
        if by_dims and not dims:
            # compute once per available dimension value of the first input
            first = m.get("input") or m.get("a") or m.get("numerator")
            for d in self.store.dims_for(first):
                if by_dims in d:
                    self.compute(mid, {by_dims: d[by_dims]})
            return
        key = (mid, json.dumps(dims, sort_keys=True))
        if key in self.results:
            return
        inputs: list[dict[str, Any]] = []
        flags: dict[str, list[str]] = {}
        extra: dict[str, pd.Series] = {}
        ptype: str | None = None

        def inp(sid: str, d: dict[str, str] | None = None):
            s, t, meta = self._get(sid, d if d is not None else dims)
            inputs.append(meta)
            return s, t

        if f == "yoy_growth":
            s, t = inp(m["input"])
            lag = int(m.get("lag", 12))
            prev = s.shift(lag) if t in ("quarterly_flow",) or lag != 12 else s.reindex([shift_months(i, -12) for i in s.index]).set_axis(s.index)
            if t == "quarterly_flow" or lag != 12:
                prev = _lag_by_months(s, lag if t != "quarterly_flow" else lag * 3)
            out = (s / prev - 1.0) * 100.0
            bad = prev.index[(prev <= 0) & prev.notna()]
            out[bad] = np.nan
            for b in bad:
                flags.setdefault(b.isoformat(), []).append("non_positive_base")
            ptype = f"{t}_growth_yoy" if t else "growth_yoy"
        elif f in ("diff", "pp_change"):
            s, t = inp(m["input"])
            lag = int(m.get("lag", 12))
            out = s - _lag_by_months(s, lag)
            ptype = t
        elif f == "share":
            nums = []
            types = []
            for sid in m["numerators"]:
                s, t = inp(sid)
                nums.append(s)
                types.append(t)
            den, td = inp(m["denominator"])
            self._check_types(mid, types + [td], m.get("types"))
            num = pd.concat(nums, axis=1).sum(axis=1, min_count=len(nums))
            out = num / den.replace(0, np.nan) * 100.0
            ptype = td
        elif f == "ratio":
            num, tn = inp(m["numerator"], m.get("dims_numerator", dims))
            den, td = inp(m["denominator"], m.get("dims_denominator", dims))
            self._check_types(mid, [tn, td], m.get("types"))
            out = num / den.replace(0, np.nan) * float(m.get("scale", 1.0))
            ptype = td
        elif f == "ratio_sum":
            num, tn = inp(m["numerator"])
            dens = []
            types = [tn]
            for sid in m["denominators"]:
                s, t = inp(sid)
                dens.append(s)
                types.append(t)
            self._check_types(mid, types, m.get("types"))
            den = pd.concat(dens, axis=1).sum(axis=1, min_count=len(dens))
            out = num / den.replace(0, np.nan) * float(m.get("scale", 1.0))
            ptype = tn
        elif f == "contribution":
            comp, tc = inp(m["component"])
            tot, tt = inp(m["total"])
            self._check_types(mid, [tc, tt], m.get("types"))
            lag = int(m.get("lag", 12))
            out = (comp - _lag_by_months(comp, lag)) / _lag_by_months(tot, lag).replace(0, np.nan) * 100.0
            ptype = tc
        elif f == "contribution_sum":
            comps = []
            types = []
            for sid in m["components"]:
                s, t = inp(sid)
                comps.append(s)
                types.append(t)
            tot, tt = inp(m["total"])
            self._check_types(mid, types + [tt], m.get("types"))
            comp = pd.concat(comps, axis=1).sum(axis=1, min_count=1)
            lag = int(m.get("lag", 12))
            out = (comp - _lag_by_months(comp, lag)) / _lag_by_months(tot, lag).replace(0, np.nan) * 100.0
            ptype = tt
        elif f == "spread":
            a, ta = inp(m["a"])
            b, tb = inp(m["b"])
            self._check_types(mid, [ta, tb], m.get("types"))
            out = a - b
            ptype = ta
        elif f == "sum":
            parts = []
            types = []
            for sid in m["inputs"]:
                s, t = inp(sid)
                parts.append(s)
                types.append(t)
            self._check_types(mid, types, m.get("types"))
            out = pd.concat(parts, axis=1).sum(axis=1, min_count=len(parts))
            ptype = types[0]
        elif f == "real_growth":
            n, tn = inp(m["nominal"])
            p, tp = inp(m["price"])
            self._check_types(mid, [tn, tp], m.get("types"))
            out = ((1 + n / 100.0) / (1 + p / 100.0) - 1.0) * 100.0
            ptype = tn
        elif f == "ytd_to_monthly":
            s, t = inp(m["input"])
            if t != "ytd_flow":
                raise MetricConfigError(f"{mid}: ytd_to_monthly requires a ytd_flow input, got {t}")
            prev = _lag_by_months(s, 1)
            out = s - prev
            jan = [i for i in s.index if i.month == 1]
            out[jan] = s[jan]
            # a February value needs the January YTD of the same year; never subtract the previous December
            ptype = "monthly_flow"
        elif f == "index_to_growth":
            s, t = inp(m["input"])
            out = s - 100.0
            ptype = t
        elif f == "rolling_sum":
            s, t = inp(m["input"])
            w = int(m.get("window", 3))
            out = s.rolling(w, min_periods=w).sum()
            ptype = f"rolling{w}_" + (t or "")
        elif f == "roa":
            prof, tp = inp(m["profit_ytd"])
            assets, ta = inp(m["assets"], m.get("dims_assets", dims))
            if tp != "ytd_flow":
                raise MetricConfigError(f"{mid}: roa needs ytd_flow profit, got {tp}")
            vals = {}
            for d in prof.index:
                if d.month == 0:
                    continue
                months = d.month
                start = dt.date(d.year - 1, 12, 31)
                window = assets[(assets.index >= start) & (assets.index <= d)]
                if len(window) < months + 1 or pd.isna(prof[d]):
                    continue
                avg = window.mean()
                vals[d] = prof[d] * (12.0 / months) / avg * 100.0 if avg else np.nan
            out = pd.Series(vals, dtype=float).sort_index()
            ptype = "annualised_ratio"
        elif f == "ratio_decomposition":
            q, tq = inp(m["numerator"])
            l, tl = inp(m["denominator"])
            self._check_types(mid, [tq, tl], m.get("types"))
            lag = int(m.get("lag", 12))
            q0, l0 = _lag_by_months(q, lag), _lag_by_months(l, lag)
            num_eff = 100.0 * (q - q0) / l0.replace(0, np.nan)
            den_eff = 100.0 * q * (1.0 / l.replace(0, np.nan) - 1.0 / l0.replace(0, np.nan))
            total = 100.0 * (q / l.replace(0, np.nan) - q0 / l0.replace(0, np.nan))
            out = total
            extra = {"numerator_effect": num_eff, "denominator_effect": den_eff, "ratio_prior": 100.0 * q0 / l0.replace(0, np.nan),
                     "ratio_current": 100.0 * q / l.replace(0, np.nan), "numerator_prior": q0, "numerator_current": q,
                     "denominator_prior": l0, "denominator_current": l}
            resid = (num_eff + den_eff - total).abs()
            if (resid.dropna() > 1e-6).any():
                raise MetricConfigError(f"{mid}: decomposition does not reconcile")
            ptype = tq
        elif f == "ratio_quarter_aligned":
            num, tn = inp(m["numerator"])
            den, td = inp(m["denominator"])
            qn = num[[i for i in num.index if i.month in (3, 6, 9, 12)]]
            out = qn / den.reindex(qn.index).replace(0, np.nan) * float(m.get("scale", 1.0))
            ptype = "quarter_end_ratio"
        else:
            raise MetricConfigError(f"{mid}: unknown formula {f}")
        out = out.dropna() if isinstance(out, pd.Series) else out
        self.results[key] = MetricResult(metric_id=mid, dims=dims, series=out, unit=m.get("unit"), label_en=m.get("label_en"), formula=f,
                                         inputs=inputs, period_type=ptype, basis=m.get("basis"), flags=flags, extra=extra)

    # ------------------------------------------------------------------ access
    def get(self, mid: str, dims: dict[str, str] | None = None) -> MetricResult | None:
        return self.results.get((mid, json.dumps(dims or {}, sort_keys=True)))

    def value(self, mid: str, period: dt.date, dims: dict[str, str] | None = None) -> float | None:
        r = self.get(mid, dims)
        if r is None or period not in r.series.index:
            return None
        v = r.series[period]
        return None if pd.isna(v) else float(v)

    def table(self) -> pd.DataFrame:
        rows = []
        for (mid, dj), r in self.results.items():
            for d, v in r.series.items():
                rows.append({"metric_id": mid, "dims": dj, "period_end": d, "value": float(v), "unit": r.unit, "formula": r.formula,
                             "period_type": r.period_type})
        return pd.DataFrame(rows)


def _lag_by_months(s: pd.Series, months: int) -> pd.Series:
    """Value `months` earlier for each period, aligned by calendar month (not by position)."""
    idx = [shift_months(i, -months) for i in s.index]
    return pd.Series(s.reindex(idx).values, index=s.index)
