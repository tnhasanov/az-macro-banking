import datetime as dt
import json

import pandas as pd
import pytest

from azmonitor.calc.data import ObservationStore
from azmonitor.calc.metrics import MetricConfigError, MetricEngine, _lag_by_months
from azmonitor.parsers.base import Observation
from azmonitor.storage.db import Database


def _obs(sid, y, m, v, ptype="month_end_stock", dims=None, unit="AZN mln"):
    from azmonitor.util.periods import month_end

    return Observation(series_id=sid, period_end=month_end(y, m), value=v, period_type=ptype, dims=dims or {}, unit=unit, population="all_credit_institutions")


def test_replacement_file_creates_revision_vintage_and_rerun_is_idempotent(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    first = [_obs("s", 2026, 1, 100.0), _obs("s", 2026, 2, 110.0)]
    r1 = db.store_observations("ds", "ds:doc1", first)
    assert r1["n_new"] == 2 and r1["n_revisions"] == 0
    # same content again -> no duplicates, no new vintage
    r2 = db.store_observations("ds", "ds:doc1", first)
    assert r2["n_new"] == 0 and r2["n_revisions"] == 0 and r2["vintage_id"] is None
    assert len(db.current_observations(["s"])) == 2
    # replacement file revises February and adds March
    second = [_obs("s", 2026, 1, 100.0), _obs("s", 2026, 2, 111.5), _obs("s", 2026, 3, 120.0)]
    r3 = db.store_observations("ds", "ds:doc2", second)
    assert r3["n_new"] == 1 and r3["n_revisions"] == 1
    cur = {r["period_end"]: r["value"] for r in db.current_observations(["s"])}
    assert cur["2026-02-28"] == 111.5 and len(cur) == 3
    superseded = db.conn.execute("SELECT value FROM observations WHERE status='superseded'").fetchall()
    assert [r["value"] for r in superseded] == [110.0]
    # a missing marker never overwrites a published value
    r4 = db.store_observations("ds", "ds:doc3", [Observation(series_id="s", period_end=dt.date(2026, 3, 31), value=None, missing_reason="dash")])
    assert r4["n_missing_not_applied"] == 1
    assert {r["period_end"]: r["value"] for r in db.current_observations(["s"])}["2026-03-31"] == 120.0


def test_historical_information_set_vs_reconstruction(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    db.store_observations("ds", "ds:doc1", [_obs("s", 2026, 1, 100.0)])
    cutoff = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    import time

    time.sleep(1.1)
    db.store_observations("ds", "ds:doc2", [_obs("s", 2026, 1, 105.0)])
    hist = ObservationStore(db, cutoff, historical_information_set=True)
    cur = ObservationStore(db)
    assert hist.series("s").iloc[-1] == 100.0 and hist.mode == "historical_information_set"
    assert cur.series("s").iloc[-1] == 105.0 and cur.mode == "reconstructed_current_vintage"


def _engine(tmp_path, obs, cfg):
    db = Database(tmp_path / "m.sqlite")
    db.store_observations("ds", "ds:doc", obs)
    store = ObservationStore(db)
    eng = MetricEngine(store)
    eng.cfg = cfg
    eng._by_id = {m["id"]: m for m in cfg}
    return eng


def test_january_reset_when_deriving_monthly_from_ytd(tmp_path):
    obs = [_obs("p", 2025, 11, 500.0, "ytd_flow"), _obs("p", 2025, 12, 560.0, "ytd_flow"), _obs("p", 2026, 1, 50.0, "ytd_flow"), _obs("p", 2026, 2, 98.0, "ytd_flow")]
    eng = _engine(tmp_path, obs, [{"id": "p.m", "formula": "ytd_to_monthly", "input": "p"}])
    eng.compute("p.m")
    s = eng.get("p.m").series
    assert s[dt.date(2026, 1, 31)] == 50.0          # January = January YTD, never minus December
    assert s[dt.date(2026, 2, 28)] == 48.0
    assert s[dt.date(2025, 12, 31)] == 60.0


def test_growth_contributions_reconcile_and_negative_base_flagged(tmp_path):
    obs = []
    for m, (a, b) in {1: (100.0, 50.0), 13: (120.0, 40.0)}.items():
        y, mm = (2025, 1) if m == 1 else (2026, 1)
        obs += [_obs("a", y, mm, a), _obs("b", y, mm, b), _obs("t", y, mm, a + b)]
    obs += [_obs("neg", 2025, 1, -5.0), _obs("neg", 2026, 1, 3.0)]
    cfg = [{"id": "t.yoy", "formula": "yoy_growth", "input": "t"},
           {"id": "a.c", "formula": "contribution", "component": "a", "total": "t", "lag": 12},
           {"id": "b.c", "formula": "contribution", "component": "b", "total": "t", "lag": 12},
           {"id": "neg.yoy", "formula": "yoy_growth", "input": "neg"}]
    eng = _engine(tmp_path, obs, cfg)
    eng.compute_all()
    d = dt.date(2026, 1, 31)
    assert abs(eng.get("a.c").series[d] + eng.get("b.c").series[d] - eng.get("t.yoy").series[d]) < 1e-9
    assert eng.get("neg.yoy").series.empty and "non_positive_base" in eng.get("neg.yoy").flags[d.isoformat()]


def test_ratio_decomposition_reconciles(tmp_path):
    obs = [_obs("q", 2025, 7, 791.87, population_="banks"), _obs("l", 2025, 7, 28498.18), _obs("q", 2026, 7, 932.26), _obs("l", 2026, 7, 32095.33)] if False else \
        [_obs("q", 2025, 7, 791.87), _obs("l", 2025, 7, 28498.18), _obs("q", 2026, 7, 932.26), _obs("l", 2026, 7, 32095.33)]
    eng = _engine(tmp_path, obs, [{"id": "dec", "formula": "ratio_decomposition", "numerator": "q", "denominator": "l", "lag": 12}])
    eng.compute("dec")
    r = eng.get("dec")
    d = dt.date(2026, 7, 31)
    num, den, tot = r.extra["numerator_effect"][d], r.extra["denominator_effect"][d], r.series[d]
    assert abs(num + den - tot) < 1e-9
    assert abs(tot - (100 * (932.26 / 32095.33 - 791.87 / 28498.18))) < 1e-9
    assert num > 0 > den


def test_incompatible_period_types_rejected(tmp_path):
    obs = [_obs("stock", 2026, 1, 100.0, "month_end_stock"), _obs("flow", 2026, 1, 10.0, "ytd_flow")]
    eng = _engine(tmp_path, obs, [{"id": "bad", "formula": "ratio", "numerator": "stock", "denominator": "flow"}])
    with pytest.raises(MetricConfigError):
        eng.compute("bad")
    eng2 = _engine(tmp_path / "b", obs, [{"id": "bad", "formula": "ratio", "numerator": "stock", "denominator": "flow"}])
    eng2.compute_all()
    assert "bad" in eng2.errors


def test_spread_and_ldr_definitions(tmp_path):
    obs = [_obs("loan", 2026, 7, 18.96, "monthly_average_rate", {"currency": "AZN"}, "%"), _obs("dep", 2026, 7, 7.46, "monthly_average_rate", {"currency": "AZN"}, "%"),
           _obs("L", 2026, 7, 34155.08), _obs("D", 2026, 7, 44501.15)]
    cfg = [{"id": "spread", "formula": "spread", "a": "loan", "b": "dep", "by_dims": "currency"}, {"id": "ldr", "formula": "ratio", "numerator": "L", "denominator": "D", "scale": 100}]
    eng = _engine(tmp_path, obs, cfg)
    eng.compute_all()
    assert abs(eng.get("spread", {"currency": "AZN"}).series.iloc[-1] - 11.5) < 1e-9
    assert abs(eng.get("ldr").series.iloc[-1] - 76.751) < 1e-3


def test_lag_by_calendar_month_not_position():
    s = pd.Series([1.0, 2.0, 3.0], index=[dt.date(2026, 1, 31), dt.date(2026, 3, 31), dt.date(2026, 4, 30)])   # February missing
    lagged = _lag_by_months(s, 1)
    assert pd.isna(lagged[dt.date(2026, 3, 31)]) and lagged[dt.date(2026, 4, 30)] == 2.0


def test_overdue_and_npl_series_are_distinct():
    from azmonitor import config

    reg = {}
    for sid, scfg, ds in config.iter_datasets():
        for s in (ds.get("parse") or {}).get("series", []) + (ds.get("parse") or {}).get("rows", []):
            reg[s["id"]] = (ds["id"], s.get("label_en", ""), s.get("population", (ds.get("parse") or {}).get("population")))
    ov, npl = reg["cba.loans.overdue"], reg["cba.bank.npl.total"]
    assert ov[0] != npl[0] and ov[2] != npl[2]
    assert "overdue" in ov[1].lower() and "non-performing" in npl[1].lower() and "npl" not in ov[1].lower()
