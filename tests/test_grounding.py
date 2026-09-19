"""Claim-level grounding: a number is only accepted when the claim behind it holds up."""
from __future__ import annotations

import pytest

from azmonitor.narrative import claims as C
from azmonitor.narrative.commentary import bind_claims
from azmonitor.narrative.contract import block
from azmonitor.narrative.validate import apply_fallback, validate_narrative

FP = {
    "fact_pack_hash": "abc123",
    "metrics": {
        "cba.loans.total_ci.yoy": {"id": "cba.loans.total_ci.yoy", "label": "Loans, y/y", "unit": "%",
                                   "period_type": "month_end_stock_growth_yoy", "dims": {},
                                   "latest": {"period": "2026-07-31", "value": 12.88},
                                   "prior": {"period": "2026-06-30", "value": 12.07}, "change": 0.81,
                                   "doc_ids": ["d1"]},
        "cba.deposits.total.yoy": {"id": "cba.deposits.total.yoy", "label": "Deposits, y/y", "unit": "%",
                                   "period_type": "month_end_stock_growth_yoy", "dims": {},
                                   "latest": {"period": "2026-07-31", "value": 6.52},
                                   "prior": {"period": "2026-06-30", "value": 2.80}, "change": 3.73,
                                   "doc_ids": ["d2"]},
        "cba.loans.overdue_ratio": {"id": "cba.loans.overdue_ratio", "label": "Overdue ratio", "unit": "%",
                                    "period_type": "month_end_stock", "dims": {},
                                    "latest": {"period": "2026-07-31", "value": 2.117},
                                    "prior": {"period": "2025-07-31", "value": 1.750}, "change": 0.367,
                                    "doc_ids": ["d3"]},
        "cba.forecast.inflation": {"id": "cba.forecast.inflation", "label": "CBA inflation forecast", "unit": "%",
                                   "period_type": "forecast", "dims": {"scenario": "baseline", "vintage": "2026-07"},
                                   "latest": {"period": "2026-12-31", "value": 6.1}, "prior": {}, "change": None,
                                   "doc_ids": ["d4"]},
        "cba.fsr.stress.car": {"id": "cba.fsr.stress.car", "label": "Stress CAR", "unit": "%",
                               "period_type": "stress_test_projection",
                               "dims": {"scenario": "adverse", "exercise": "2025-12-31"},
                               "latest": {"period": "2027-12-31", "value": 13.6}, "prior": {}, "change": None,
                               "doc_ids": ["d5"]},
    },
    "slides": {},
}
PASSAGES = {"p1": {"text": "Risks to the inflation outlook remain tilted to the upside, reflecting rising energy prices.",
                   "cite": "page 1", "doc_id": "d9", "publication_id": "policy_decision:1"}}


def narrative(slide_block, **kw):
    base = {"fact_pack_hash": "abc123", "cover": {"headline": ""}, "findings": [], "questions": [],
            "slides": {"M08": {"interpretations": [slide_block]}}}
    base.update(kw)
    return base


def problems(nar, fp=FP):
    return validate_narrative(nar, fp, PASSAGES)["problems"]


def test_a_correct_claim_passes():
    nar = narrative(block("Loans grew 12.9% y/y.", ["cba.loans.total_ci.yoy@2026-07-31#yoy"]))
    assert problems(nar) == []


def test_a_number_bound_to_the_wrong_metric_is_rejected():
    """6.5 is a real number in the pack, but it is deposit growth, not loan growth."""
    nar = narrative(block("Loans grew 6.5% y/y.", ["cba.loans.total_ci.yoy@2026-07-31#yoy"]))
    kinds = {p["kind"] for p in problems(nar)}
    assert "unbound" in kinds


def test_a_number_bound_to_the_wrong_period_is_rejected():
    nar = narrative(block("Loans grew 12.9% y/y in June.", ["cba.loans.total_ci.yoy@2026-06-30#yoy"]))
    found = problems(nar)
    assert any(p["kind"] == "unbound" for p in found)


def test_a_claim_for_a_period_the_metric_does_not_publish_is_rejected():
    nar = narrative(block("Loans grew 12.9% y/y.", ["cba.loans.total_ci.yoy@2026-05-31#yoy"]))
    found = problems(nar)
    assert any(p["kind"] == "claim" and "not for 2026-05-31" in p["issue"] for p in found)


def test_a_percentage_point_move_may_not_be_written_as_a_percentage():
    nar = narrative(block("The overdue ratio rose 0.4% over the year.",
                          ["cba.loans.overdue_ratio@2026-07-31#change"]))
    found = problems(nar)
    assert any(p["kind"] in ("unit", "unbound") for p in found)


def test_the_same_move_written_in_percentage_points_passes():
    nar = narrative(block("The overdue ratio rose 0.4 pp over the year.",
                          ["cba.loans.overdue_ratio@2026-07-31#change"]))
    assert problems(nar) == []


def test_a_direction_word_contradicting_the_sign_is_rejected():
    nar = narrative(block("Deposit growth fell 3.7 pp over the year.",
                          ["cba.deposits.total.yoy@2026-07-31#change"]))
    found = problems(nar)
    assert any(p["kind"] == "direction" for p in found)


def test_a_small_number_still_has_to_be_grounded():
    """0.4 pp on the overdue ratio is a financial claim, not noise to be waved through."""
    nar = narrative(block("The overdue ratio rose 0.4 pp over the year."))
    found = problems(nar)
    assert any(p["kind"] == "unbound" and p["number"] == 0.4 for p in found)


def test_a_literal_needs_a_stated_reason_and_then_passes():
    nar = narrative(block("Three findings are listed on this slide.",
                          literals=[{"value": 3, "reason": "count of findings"}]))
    assert problems(nar) == []


def test_rounding_beyond_the_written_precision_is_rejected():
    nar = narrative(block("Loans grew 13.4% y/y.", ["cba.loans.total_ci.yoy@2026-07-31#yoy"]))
    assert any(p["kind"] == "unbound" for p in problems(nar))


def test_a_stale_fact_pack_hash_rejects_the_whole_narrative():
    nar = narrative(block("Loans grew 12.9% y/y.", ["cba.loans.total_ci.yoy@2026-07-31#yoy"]))
    nar["fact_pack_hash"] = "written-for-an-older-pack"
    nar["findings"] = [{"id": "F1", "slide_id": "M08", "classification": "observed_fact", "statement": "x"}]
    result = validate_narrative(nar, FP, PASSAGES)
    assert result["stale"] is True and result["ok"] is False
    assert result["rejected_slides"] == ["M08"] and result["rejected_findings"] == ["F1"]
    fallback = narrative(block("Facts-only text."))
    merged = apply_fallback(nar, fallback, result)
    assert merged["stale_fact_pack"] is True and merged["fallback_scope"] == "whole narrative"


def test_a_narrative_without_a_hash_is_rejected():
    nar = narrative(block("Loans grew 12.9% y/y.", ["cba.loans.total_ci.yoy@2026-07-31#yoy"]))
    nar.pop("fact_pack_hash")
    assert validate_narrative(nar, FP, PASSAGES)["stale"] is True


def test_a_forecast_may_not_be_written_as_an_outcome():
    """The value is right, but claiming it as a level presents a projection as a measurement."""
    nar = narrative(block("Inflation was 6.1%.",
                          ['cba.forecast.inflation|{"scenario": "baseline", "vintage": "2026-07"}@2026-12-31#level']))
    found = problems(nar)
    assert any(p["kind"] == "claim" and "'level'" in p["issue"] for p in found)


def test_a_forecast_written_as_a_forecast_passes():
    nar = narrative(block("The Central Bank projects 6.1% at end-2026.",
                          ['cba.forecast.inflation|{"scenario": "baseline", "vintage": "2026-07"}@2026-12-31#forecast']))
    assert problems(nar) == []


def test_a_stress_result_may_not_be_written_as_a_forecast():
    nar = narrative(block("Capital adequacy is forecast at 13.6% in 2027.",
                          ['cba.fsr.stress.car|{"scenario": "adverse", "exercise": "2025-12-31"}@2027-12-31#forecast']))
    found = problems(nar)
    assert any(p["kind"] == "claim" for p in found)


def test_a_stress_result_written_as_a_stress_projection_passes():
    nar = narrative(block("Under the adverse scenario capital adequacy is projected at 13.6% by 2027.",
                          ['cba.fsr.stress.car|{"scenario": "adverse", "exercise": "2025-12-31"}@2027-12-31#stress']))
    assert problems(nar) == []


def test_a_quotation_must_appear_in_the_cited_passage():
    good = block("The Bank says risks are tilted to the upside.",
                 quotes=[{"passage_id": "p1", "text": "remain tilted to the upside"}])
    bad = block("The Bank says risks are balanced.",
                quotes=[{"passage_id": "p1", "text": "risks are broadly balanced"}])
    assert problems(narrative(good)) == []
    assert any(p["kind"] == "quote" for p in problems(narrative(bad)))


def test_a_statement_attributed_to_the_central_bank_must_cite_a_passage():
    nar = narrative(block("text"))
    nar["findings"] = [{"id": "F1", "slide_id": "M08", "classification": "cba_assessment",
                        "statement": block("The Central Bank sees upside risks to inflation.")}]
    assert any(p["kind"] == "quote" for p in problems(nar))


def test_dimensions_that_do_not_match_the_published_slice_are_rejected():
    nar = narrative(block("The adverse path reaches 13.6%.",
                          ['cba.fsr.stress.car|{"scenario": "baseline", "exercise": "2025-12-31"}@2027-12-31#stress']))
    assert any(p["kind"] == "claim" for p in problems(nar))


def test_years_dates_and_table_numbers_are_not_read_as_measurements():
    text = "On 31 July 2026 the Bank held the rate; see CBA table 2.11 and slide 14."
    assert C.numbers_in(text) == []


def test_bind_claims_reports_what_it_cannot_decide():
    nar = narrative(block("Loans grew 12.9% y/y and something grew 99.9%."))
    bound, report = bind_claims(nar, FP)
    assert report["bound"] >= 1
    assert any(u["written"] == "99.9%" for u in report["unsupported"])
