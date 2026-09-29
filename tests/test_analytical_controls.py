"""The analytical defects this system has had, each pinned so it cannot come back unnoticed.

Six, named in the requirements:

  1. the consumer-loan NPL ratio presented as the banking sector's NPL ratio;
  2. forecast revisions compared across rounds that are not comparable;
  3. a historical backfill announced as a new release;
  4. deposit growth explained without financial corporations;
  5. an attributed statement with no evidence passage behind it;
  6. a file's upload date used as its publication date.

Where an older test already pins one, it is named here rather than duplicated, and this file
checks that it still exists — deleting it would silently remove the control.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from azmonitor import config
from azmonitor.narrative.contract import block
from azmonitor.narrative.validate import validate_narrative

HERE = Path(__file__).parent

FP = {
    "fact_pack_hash": "h1",
    "metrics": {
        "cba.bank.npl.ratio": {"id": "cba.bank.npl.ratio", "label": "NPL ratio (NPL / bank loan portfolio)",
                               "unit": "%", "period_type": "month_end_stock", "dims": {},
                               "latest": {"period": "2026-07-31", "value": 2.90},
                               "prior": {"period": "2025-07-31", "value": 3.10}, "change": -0.20, "doc_ids": ["d1"]},
        "cba.bank.npl.ratio_consumer": {"id": "cba.bank.npl.ratio_consumer", "label": "Consumer-loan NPL ratio",
                                        "unit": "%", "period_type": "month_end_stock", "dims": {},
                                        "latest": {"period": "2026-07-31", "value": 3.70},
                                        "prior": {"period": "2025-07-31", "value": 3.40}, "change": 0.30,
                                        "doc_ids": ["d1"]},
        "cba.deposits.hh.contrib": {"id": "cba.deposits.hh.contrib", "label": "Household contribution", "unit": "pp",
                                    "period_type": "contribution", "dims": {},
                                    "latest": {"period": "2026-07-31", "value": 5.8}, "prior": {}, "change": None,
                                    "doc_ids": ["d2"]},
    },
    "slides": {},
}


def _problems(text: str, claims: list[str]) -> list[dict]:
    nar = {"fact_pack_hash": "h1", "cover": {"headline": ""}, "findings": [], "questions": [],
           "slides": {"M10": {"interpretations": [block(text, claims)]}}}
    return validate_narrative(nar, FP, {})["problems"]


# ------------------------------------------------------------------ 1. population and definition

def test_the_consumer_npl_ratio_cannot_be_written_as_the_banking_sectors():
    wrong = _problems("The banking-sector NPL ratio was 3.7% in July.",
                      ["cba.bank.npl.ratio_consumer@2026-07-31#level"])
    assert [p["kind"] for p in wrong] == ["definition"]
    assert "consumer-loan figure" in wrong[0]["issue"]


def test_the_consumer_npl_ratio_written_as_consumer_passes():
    assert _problems("The consumer NPL ratio was 3.7% in July.", ["cba.bank.npl.ratio_consumer@2026-07-31#level"]) == []


def test_the_sector_ratio_may_be_called_the_sectors():
    assert _problems("The banking-sector NPL ratio was 2.9% in July.", ["cba.bank.npl.ratio@2026-07-31#level"]) == []


def test_a_segment_mentioned_elsewhere_in_the_sentence_does_not_excuse_the_mislabel():
    wrong = _problems("Consumer lending grew, and the overall NPL ratio was 3.7%.",
                      ["cba.bank.npl.ratio_consumer@2026-07-31#level"])
    assert [p["kind"] for p in wrong] == ["definition"]


def test_household_contributions_are_not_the_total():
    wrong = _problems("Aggregate deposit growth was 5.8 pp.", ["cba.deposits.hh.contrib@2026-07-31#contribution"])
    assert [p["kind"] for p in wrong] == ["definition"]


def test_every_npl_segment_series_says_it_is_a_segment():
    labels = {}
    for _sid, _src, ds in config.iter_datasets():
        for row in (ds.get("parse") or {}).get("rows") or []:
            if row["id"].startswith("cba.bank.npl."):
                labels[row["id"]] = row.get("label_en") or ""
    for sid in ("cba.bank.npl.consumer", "cba.bank.npl.business", "cba.bank.npl.mortgage",
                "cba.bank.npl.ratio_consumer", "cba.bank.npl.ratio_business", "cba.bank.npl.ratio_mortgage"):
        segment = sid.rsplit("_", 1)[-1].rsplit(".", 1)[-1]
        assert segment in labels[sid].lower(), f"{sid} is labelled {labels[sid]!r}, which does not name its segment"
    assert "portfolio" in labels["cba.bank.npl.ratio"].lower()


# ------------------------------------------------------------------ 4. deposits

def test_deposit_growth_is_decomposed_into_all_three_holders():
    metrics = {m["id"]: m for m in config.metrics_config()["metrics"]}
    for holder in ("hh", "nfc", "fin"):
        m = metrics[f"cba.deposits.{holder}.contrib"]
        assert m["total"] == "cba.deposits.total" and m["component"] == f"cba.deposits.{holder}.total"
    # the source table's own check: total = households + financial + non-financial corporations
    checks = [c for _s, _src, ds in config.iter_datasets() if ds["id"] == "cba_deposits"
              for c in (ds.get("parse") or {}).get("checks") or (ds.get("checks") or [])]
    sums = [c for c in checks if c.get("type") == "components_sum" and c.get("total") == "cba.deposits.total"]
    assert sums and set(sums[0]["parts"]) == {"cba.deposits.hh.total", "cba.deposits.fin.total", "cba.deposits.nfc.total"}


def test_the_deposit_slide_carries_the_financial_corporations_row():
    source = (HERE.parent / "azmonitor" / "facts.py").read_text()
    assert '("fin", "Financial corporations")' in source


def test_contributions_of_the_three_holders_sum_to_total_growth(tmp_path):
    from azmonitor.calc.data import ObservationStore
    from azmonitor.calc.metrics import MetricEngine
    from azmonitor.parsers.base import Observation
    from azmonitor.storage.db import Database
    from azmonitor.util.periods import month_end

    values = {2025: {"hh": 10_000.0, "nfc": 6_000.0, "fin": 2_000.0}, 2026: {"hh": 11_580.0, "nfc": 5_742.0, "fin": 2_560.0}}
    obs = []
    for y, parts in values.items():
        for k, v in parts.items():
            obs.append(Observation(series_id=f"cba.deposits.{k}.total", period_end=month_end(y, 7), value=v,
                                   period_type="month_end_stock", unit="AZN mln"))
        obs.append(Observation(series_id="cba.deposits.total", period_end=month_end(y, 7), value=sum(parts.values()),
                               period_type="month_end_stock", unit="AZN mln"))
    db = Database(tmp_path / "d.sqlite")
    db.store_observations("cba_deposits", "cba_deposits:doc", obs)
    eng = MetricEngine(ObservationStore(db))
    cfg = [m for m in config.metrics_config()["metrics"]
           if m["id"] in ("cba.deposits.total.yoy", "cba.deposits.hh.contrib", "cba.deposits.nfc.contrib",
                          "cba.deposits.fin.contrib")]
    eng.cfg, eng._by_id = cfg, {m["id"]: m for m in cfg}
    eng.compute_all()
    d = dt.date(2026, 7, 31)
    total = eng.get("cba.deposits.total.yoy").series[d]
    parts = sum(eng.get(f"cba.deposits.{k}.contrib").series[d] for k in ("hh", "nfc", "fin"))
    assert abs(parts - total) < 1e-9
    assert eng.get("cba.deposits.fin.contrib").series[d] > 3.0      # leaving it out would misstate growth by 3 pp


# ------------------------------------------------------------------ 2, 3, 5, 6: pinned elsewhere

PINNED = {
    "comparable forecast revisions": ("test_publications.py", 'rows["2027-12-31"]["comparable"] is False'),
    "backfill classified, never announced": ("test_changes.py", "historical_backfill"),
    "backfill publishes nothing and emails nobody": ("test_worker.py", "test_translations_and_backfills_are_recorded_and_announce_nothing"),
    "an attributed statement must quote an evidence passage": ("test_grounding.py", "test_a_quotation_must_appear_in_the_cited_passage"),
    "upload date is not a publication date": ("test_publications.py", "HTTP Last-Modified header of the file"),
    "a new release is judged on its release date": ("test_changes.py", "published_at"),
}


@pytest.mark.parametrize("what", sorted(PINNED))
def test_the_control_is_still_pinned(what):
    filename, marker = PINNED[what]
    assert marker in (HERE / filename).read_text(), f"the regression test for {what} has gone from {filename}"


def test_a_cba_assessment_without_a_passage_is_rejected():
    nar = {"fact_pack_hash": "h1", "cover": {"headline": ""}, "questions": [], "slides": {},
           "findings": [{"id": "f1", "slide_id": "M10", "classification": "cba_assessment",
                         "statement": block("The CBA considers risks balanced.", [])}]}
    result = validate_narrative(nar, FP, {})
    assert "f1" in result["rejected_findings"]
    assert any(p["kind"] == "quote" for p in result["problems"])
