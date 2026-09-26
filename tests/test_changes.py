"""Classifying source changes, and deciding which reports they affect.

The cases here are the ones the brief names and the ones that went wrong before: a backfill of old
publications read as a stream of new releases; an English translation weeks after the Azerbaijani
original read as a second release; a re-downloaded file whose bytes changed read as new data.
"""
from __future__ import annotations

import datetime as dt

from azmonitor import changes as C
from azmonitor import config

TODAY = dt.date(2026, 9, 26)


def rec(**kw):
    base = {"dataset_id": "cba_loans_by_institution", "document_id": "doc1", "pub_type": None,
            "is_translation": False, "new_publication": False, "n_new": 0, "n_revisions": 0,
            "new_periods": [], "revised_periods": [], "latest_before": "2026-07-31"}
    base.update(kw)
    return base


# ------------------------------------------------------------------ classification

def test_a_new_month_is_new_observations():
    cls, reason = C.classify(rec(n_new=40, new_periods=["2026-08-31"]), today=TODAY)
    assert cls == "new_observations" and "2026-08-31" in reason


def test_a_correction_to_published_months_is_a_substantive_revision():
    cls, _ = C.classify(rec(n_revisions=3, revised_periods=["2026-06-30", "2026-07-31"]), today=TODAY)
    assert cls == "substantive_revision"


def test_a_new_month_with_revisions_is_led_by_the_new_month():
    cls, _ = C.classify(rec(n_new=40, new_periods=["2026-08-31"], n_revisions=2,
                            revised_periods=["2026-07-31"]), today=TODAY)
    assert cls == "new_observations"


def test_changed_bytes_with_unchanged_data_are_bytes_only():
    assert C.classify(rec(), today=TODAY)[0] == "bytes_only"


def test_old_months_filled_in_are_a_backfill_not_a_release():
    cls, _ = C.classify(rec(n_new=12, new_periods=["2019-01-31", "2019-02-28"]), today=TODAY)
    assert cls == "historical_backfill"


def test_a_first_load_of_a_dataset_is_a_backfill():
    """A fresh store collects years of history in its first run; none of it is news."""
    cls, _ = C.classify(rec(n_new=500, new_periods=["2020-01-31", "2026-08-31"], latest_before=None), today=TODAY)
    assert cls == "historical_backfill"


def test_a_recent_publication_is_a_new_release():
    cls, _ = C.classify(rec(dataset_id="cba_policy_decisions", pub_type="policy_decision",
                            new_publication=True, published_at="2026-09-24"), today=TODAY)
    assert cls == "new_publication"


def test_an_old_publication_seen_for_the_first_time_is_a_backfill():
    """The failure this guards: a backfill of monetary policy reviews read as a stream of releases."""
    cls, reason = C.classify(rec(dataset_id="cba_monetary_policy_review", pub_type="monetary_policy_review",
                                 new_publication=True, published_at="2024-02-15"), today=TODAY)
    assert cls == "historical_backfill" and "2024-02-15" in reason


def test_a_publication_with_no_release_date_is_not_assumed_new():
    """The day we downloaded it is not the day it was released."""
    cls, _ = C.classify(rec(pub_type="financial_stability_report", new_publication=True,
                            published_at=None, first_seen_at="2026-09-26T05:15:00Z"), today=TODAY)
    assert cls == "historical_backfill"


def test_a_translation_is_never_a_release():
    cls, reason = C.classify(rec(dataset_id="cba_policy_decisions", pub_type="policy_decision",
                                 is_translation=True, language="en", original_language="az",
                                 new_publication=False), today=TODAY)
    assert cls == "translation" and "not a release" in reason


def test_the_windows_are_per_publication_type():
    """A decision is news for days; a stability report for weeks."""
    decision = rec(pub_type="policy_decision", new_publication=True, published_at="2026-09-10")
    fsr = rec(pub_type="financial_stability_report", new_publication=True, published_at="2026-09-10")
    assert C.classify(decision, today=TODAY)[0] == "historical_backfill"
    assert C.classify(fsr, today=TODAY)[0] == "new_publication"


# ------------------------------------------------------------------ mapping

def test_every_dataset_is_mapped_or_deliberately_unmapped():
    cfg = C.triggers()
    decided = set(cfg.get("datasets") or {}) | set(cfg.get("unmapped") or {})
    pub_types = set(cfg.get("publication_types") or {})
    missing = [ds["id"] for _, _, ds in config.iter_datasets()
               if ds["id"] not in decided and ds.get("pub_type") not in pub_types]
    assert not missing, f"datasets with no decision in config/triggers.yaml: {missing}"


def test_the_monthly_anchors_and_companions_all_trigger_the_monthly():
    cfg = C.triggers()
    monthly = config.reports_config()["monthly"]
    anchors = [d for ids in monthly["anchors"].values() for d in ids]
    for dataset in anchors + list(monthly.get("companions") or []):
        reports = [r["report"] for r in (cfg["datasets"].get(dataset) or [])]
        assert "monthly" in reports, f"{dataset} feeds the monthly but does not trigger it"


def test_banking_statistics_affect_the_monthly_and_the_sector_reviews_that_read_them():
    aff = C.affects(rec(dataset_id="cba_loans_by_sector"), "new_observations",
                    sectors=["construction", "agriculture"])
    assert {"report": "monthly", "announce": True} in aff
    assert [a["sector"] for a in aff if a["report"] == "sector"] == ["construction", "agriculture"]


def test_a_deposits_table_does_not_regenerate_a_sector_review():
    aff = C.affects(rec(dataset_id="cba_deposits"), "new_observations", sectors=["construction"])
    assert [a["report"] for a in aff] == ["monthly"]


def test_a_policy_decision_announces_the_decision_update_and_quietly_refreshes_the_monthly():
    aff = C.affects(rec(dataset_id="cba_policy_decisions", pub_type="policy_decision", publication_id="dec:2026-09-24"),
                    "new_publication")
    assert {"report": "decision_update", "publication_id": "dec:2026-09-24", "announce": True} in aff
    assert {"report": "monthly", "announce": False} in aff


def test_nothing_is_affected_by_a_backfill_a_translation_or_a_bytes_only_change():
    for cls in ("historical_backfill", "translation", "bytes_only"):
        assert C.affects(rec(), cls) == []


# ------------------------------------------------------------------ planning

def _changes(records):
    refresh = {"datasets": {}}
    for r in records:
        refresh["datasets"].setdefault(r["dataset_id"], {"changes": []})["changes"].append(r)
    return C.classify_refresh(refresh, batch_id="job_test", today=TODAY)


def test_a_dozen_monthly_tables_in_one_check_plan_the_monthly_once(monkeypatch):
    monkeypatch.setattr(C, "scheduled_sectors", lambda: ["construction"])
    tables = ["cba_loans_by_institution", "cba_deposits", "cba_bank_balance", "cba_bank_pnl", "cba_bank_npl",
              "cba_rates_new", "cba_loans_by_sector"]
    planned = C.plan(_changes([rec(dataset_id=t, document_id=t, n_new=10, new_periods=["2026-08-31"])
                               for t in tables]))
    assert [(p.report_type, p.scope) for p in planned] == [("monthly", "latest"), ("sector", "construction")]
    assert len(planned[0].change_ids) == len(tables) and planned[0].cause == "new_data"


def test_a_batch_of_only_backfills_plans_nothing():
    planned = C.plan(_changes([rec(n_new=5, new_periods=["2018-01-31"]),
                               rec(dataset_id="cba_deposits", document_id="d2", latest_before=None,
                                   n_new=300, new_periods=["2020-01-31"])]))
    assert planned == []


def test_a_decision_alone_announces_one_report():
    planned = C.plan(_changes([rec(dataset_id="cba_policy_decisions", pub_type="policy_decision",
                                   publication_id="dec:2026-09-24", new_publication=True,
                                   published_at="2026-09-24")]))
    announced = [p.report_type for p in planned if p.announce]
    assert announced == ["decision_update"]
    refreshed = [p for p in planned if p.report_type == "monthly"][0]
    assert refreshed.cause == "coverage_refresh" and not refreshed.announce


def test_a_decision_and_new_monthly_data_together_announce_the_monthly_too():
    planned = C.plan(_changes([
        rec(dataset_id="cba_policy_decisions", pub_type="policy_decision", publication_id="dec:1",
            new_publication=True, published_at="2026-09-24"),
        rec(dataset_id="cba_deposits", document_id="d2", n_new=4, new_periods=["2026-08-31"])]))
    monthly = [p for p in planned if p.report_type == "monthly"][0]
    assert monthly.announce and monthly.cause == "new_publication"


def test_the_cause_is_the_most_newsworthy_of_the_changes_behind_it():
    planned = C.plan(_changes([
        rec(document_id="a", n_revisions=2, revised_periods=["2026-06-30"]),
        rec(dataset_id="cba_deposits", document_id="b", n_new=3, new_periods=["2026-08-31"])]))
    assert planned[0].cause == "new_data"


def test_a_correction_alone_plans_a_revision():
    planned = C.plan(_changes([rec(n_revisions=2, revised_periods=["2026-06-30"])]))
    assert [(p.report_type, p.cause) for p in planned] == [("monthly", "revision")]
