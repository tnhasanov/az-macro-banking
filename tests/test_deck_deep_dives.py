"""The deck's second slide behind each theme, its finding titles and its footnotes.

The August 2026 deck was edited by hand into this shape: a title that states the finding, a deep
dive after each banking theme, and data-quality notes in the footers. These tests hold the
generated deck to it without the hand edits: every number in a title is a claim the validator
resolves, and an older fact pack without the deep dives still renders.
"""
import datetime as dt
import json
from pathlib import Path

from azmonitor import config
from azmonitor.calc.data import ObservationStore
from azmonitor.calc.metrics import MetricEngine
from azmonitor.narrative.titles import finding_titles
from azmonitor.narrative.validate import _check_block
from azmonitor.parsers.base import Observation
from azmonitor.render.monthly import MonthlyRenderer
from azmonitor.storage.db import Database

FIXTURE = Path(__file__).parent / "fixtures" / "fact_pack_min.json"


def _engine(tmp_path, obs, cfg, cutoff=None):
    db = Database(tmp_path / "m.sqlite")
    db.store_observations("ds", "ds:doc", obs)
    eng = MetricEngine(ObservationStore(db), cutoff=cutoff)
    eng.cfg = cfg
    eng._by_id = {m["id"]: m for m in cfg}
    return eng


def test_the_policy_rate_is_read_at_each_month_end_from_the_last_decision(tmp_path):
    obs = [Observation(series_id="rate", period_end=d, value=v, period_type="policy_rate_effective", unit="%")
           for d, v in ((dt.date(2025, 12, 10), 6.75), (dt.date(2026, 2, 4), 6.5))]
    eng = _engine(tmp_path, obs, [{"id": "rate.m", "formula": "month_end_step", "input": "rate"}], cutoff=dt.date(2026, 4, 15))
    eng.compute("rate.m")
    s = eng.get("rate.m").series
    assert list(s.index) == [dt.date(2025, 12, 31), dt.date(2026, 1, 31), dt.date(2026, 2, 28), dt.date(2026, 3, 31)]
    assert list(s.values) == [6.75, 6.75, 6.5, 6.5]


def test_real_loan_growth_deflates_by_consumer_prices_rather_than_subtracting_them():
    cfg = next(m for m in config.metrics_config()["metrics"] if m["id"] == "cba.loans.total_ci.real.yoy")
    assert cfg["formula"] == "real_growth" and cfg["price"] == "ssc.cpi.all.yoy"
    # 14.5% nominal and 5.7% inflation is 8.3% in real terms, not 8.8%
    assert round(((1 + 14.507 / 100) / (1 + 5.7 / 100) - 1) * 100, 1) == 8.3


def _m(mid, latest, period, *, prior=None, prior_period=None, change=None, compare="lag12", ptype="month_end_stock", unit="%", dims=None):
    e = {"id": mid, "dims": dims or {}, "unit": unit, "period_type": ptype, "compare": compare,
         "latest": {"period": period, "value": latest}, "prior": None, "change": change}
    if prior is not None:
        e["prior"] = {"period": prior_period, "value": prior}
    return e


def _pack():
    azn = {"currency": "AZN"}
    metrics = {
        "cba.loans.total_ci.yoy": _m("cba.loans.total_ci.yoy", 9.12, "2026-08-31", prior=11.1, prior_period="2026-07-31", change=-1.98,
                                     compare="lag1", ptype="month_end_stock_growth_yoy"),
        "cba.loans.sector.households.contrib": _m("cba.loans.sector.households.contrib", 5.04, "2026-08-31", compare="lag1", unit="pp"),
        'cba.rates.new.spread|{"currency": "AZN"}': _m("cba.rates.new.spread", 10.73, "2026-08-31", prior=11.5, prior_period="2025-08-31",
                                                       change=-0.77, ptype="monthly_average_rate", unit="pp", dims=azn),
        'cba.rates.new.deposit|{"currency": "AZN"}': _m("cba.rates.new.deposit", 8.1, "2026-08-31", prior=7.02, prior_period="2025-08-31",
                                                        change=1.08, ptype="monthly_average_rate", dims=azn),
        'cba.rates.new.loan|{"currency": "AZN"}': _m("cba.rates.new.loan", 18.83, "2026-08-31", ptype="monthly_average_rate", dims=azn),
        "cba.policy.rate.month_end": _m("cba.policy.rate.month_end", 6.5, "2026-08-31", prior=7.0, prior_period="2025-08-31", change=-0.5,
                                        ptype="month_end_level"),
        "cba.money.m2.yoy": _m("cba.money.m2.yoy", 20.68, "2026-08-31", ptype="month_end_stock_growth_yoy"),
        "cba.policy.rate": _m("cba.policy.rate", 6.5, "2026-09-23", prior=6.5, prior_period="2026-07-31", change=0.0,
                              compare="previous_observation", ptype="policy_rate_effective"),
        "cba.policy.corridor_floor": _m("cba.policy.corridor_floor", 5.0, "2026-09-23", prior=5.5, prior_period="2026-07-31",
                                        change=-0.5, compare="previous_observation", ptype="policy_rate_effective"),
        "cba.policy.corridor_ceiling": _m("cba.policy.corridor_ceiling", 7.5, "2026-09-23", prior=7.5, prior_period="2026-07-31",
                                          change=0.0, compare="previous_observation", ptype="policy_rate_effective"),
    }
    return {"metrics": metrics, "slides": {}}


def test_every_number_in_a_finding_title_is_a_claim_the_validator_resolves():
    fp = _pack()
    titles = finding_titles(fp)
    assert {"M08", "M13", "M28", "M19"} <= set(titles)
    for sid, b in titles.items():
        problems = []
        ok, n = _check_block(sid, b, fp, {}, problems)
        assert ok and n >= 2, (sid, b, problems)


def test_a_title_says_what_moved_in_the_direction_it_moved():
    t = {sid: (b["text"] if isinstance(b, dict) else b) for sid, b in finding_titles(_pack()).items()}
    assert t["M08"] == "Loan growth was 9.1% y/y at end-Aug 2026, 2.0 pp lower than a month earlier; households contributed 5.0 pp"
    assert t["M13"].startswith("The AZN pricing spread narrowed 0.8 pp in a year to 10.7 pp")
    assert "down 50 bp in a year" in t["M28"] and "rose 1.1 pp to 8.1%" in t["M28"]
    # a corridor move with the rate held is written as exactly that, never as a rate cut
    assert t["M19"] == ("The CBA kept the refinancing rate at 6.50% on 23 September 2026 and cut the corridor floor 50 bp to 5.00%")


def _renderer(fp):
    theme = dict(config.theme())
    theme["_root"] = str(config.ROOT)
    return MonthlyRenderer(fp, {"mode": "facts_only", "slides": {}, "findings": []}, theme, "en")


def test_a_fact_pack_without_the_deep_dives_still_renders_and_numbers_its_pages():
    fp = json.loads(FIXTURE.read_text())
    r = _renderer(fp)
    pages = r.page_map()
    assert not set(MonthlyRenderer.DEEP_DIVES) & set(pages)
    assert pages["M08"] + 1 == pages["M09"]
    fp["slides"]["M23"] = {"kpis": []}
    assert _renderer(fp).page_map()["M23"] == pages["M08"] + 1      # the deep dive follows its theme


def test_quality_notes_name_the_dataset_once_and_say_the_shown_periods_reconcile():
    fp = json.loads(FIXTURE.read_text())
    fp["quality"] = {"checks": [
        {"check": "components_sum:cba_deposits", "ok": False, "severity": "warning", "failed_periods": ["2015-12-31", "2016-01-31"]},
        {"check": "contributions_reconcile:cba.deposits", "ok": False, "severity": "warning",
         "failed_periods": ["2015-12-31", "2016-01-31", "2016-12-31", "2017-01-31"]},
        {"check": "components_sum:cba_loans_by_institution", "ok": True, "severity": "warning", "failed_periods": []}]}
    notes = _renderer(fp)._data_notes(["cba_deposits"])
    assert len(notes) == 1
    assert notes[0].startswith("Quality check, deposits and savings in credit institutions: the component sum fails in 2 historical periods")
    assert "the contribution reconciliation fails in 4 historical periods (2015-12, 2016-01, …)" in notes[0]
    assert notes[0].endswith("the periods shown reconcile.")
    assert _renderer(fp)._data_notes(["cba_loans_by_institution"]) == []


def test_quality_notes_for_several_datasets_are_one_sentence_so_the_footer_stays_on_the_page():
    fp = json.loads(FIXTURE.read_text())
    fp["quality"] = {"checks": [
        {"check": "components_sum:cba_deposits", "ok": False, "severity": "warning", "failed_periods": ["2015-12-31", "2016-01-31"]},
        {"check": "contributions_reconcile:cba.deposits", "ok": False, "severity": "warning",
         "failed_periods": ["2015-12-31", "2016-01-31", "2016-12-31", "2017-01-31"]},
        {"check": "components_sum:cba_loans_by_institution", "ok": False, "severity": "warning", "failed_periods": ["2006-01-31"]}]}
    notes = _renderer(fp)._data_notes(["cba_deposits", "cba_loans_by_institution"])
    assert len(notes) == 1
    assert "deposits and savings in credit institutions (6 periods)" in notes[0]
    assert "structure of loans to the economy (1 period)" in notes[0]
    assert notes[0].endswith("the periods shown reconcile.")


def test_long_commentary_is_set_smaller_rather_than_cut():
    from azmonitor.render.monthly import _fit
    assert _fit(["A short line."], 4.43, 1.9, (9.5, 9.0, 8.5)) == 9.5
    assert _fit(["word " * 50] * 3, 4.43, 1.9, (9.5, 9.0, 8.5)) == 9.0
    assert _fit(["word " * 120], 4.43, 0.8, (9.5, 9.0, 8.5)) == 8.5


def test_a_held_rate_with_a_lower_floor_puts_the_floor_move_on_the_card():
    from azmonitor.render.policy_slides import change_card
    held = {"floor_change_bp": -50, "ceiling_change_bp": 0, "corridor_change": "floor cut 50 bp"}
    assert change_card(held, {"rate_change_bp": 0.0}) == ("-50 bp", "Change at this meeting: corridor floor",
                                                          "rate and ceiling unchanged")
    cut = {"floor_change_bp": -25, "ceiling_change_bp": -25, "corridor_change": "all cut 25 bp"}
    assert change_card(cut, {"rate_change_bp": -25.0, "label": "cut"}) == ("-25 bp", "Change in the rate at this meeting",
                                                                         "cut; corridor all cut 25 bp")
