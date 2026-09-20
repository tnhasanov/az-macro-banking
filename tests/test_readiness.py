"""Whether a report may be produced, and why not when it may not.

These rules are the difference between a system that publishes on a schedule and one that publishes
when there is something worth publishing. The cases below are the ones that decide that: a month
that arrived incomplete, a publisher that went quiet, an archive loaded in bulk, and a file that
downloaded but did not extract.
"""
from __future__ import annotations

import datetime as dt

from azmonitor.scheduling import readiness as R
from azmonitor.storage.db import Database

TODAY = dt.date(2026, 9, 19)

MONTHLY_RULES = {
    "required_datasets": ["cba_loans_by_institution", "cba_deposits"],
    "companion_datasets": ["cba_bank_pnl"],
    "companion_grace_days": 7,
    "max_days_since_source_published": 45,
    "require_quality_pass": True,
}


def _db(tmp_path) -> Database:
    return Database(tmp_path / "t.sqlite")


def _dataset(db, dsid: str, period: str, published: str, status: str = "parsed") -> None:
    db.set_dataset_state(dsid, source_id="CBA", latest_period_end=period, status=status)
    db.upsert_document({"doc_id": f"{dsid}:{period}", "source_id": "CBA", "dataset_id": dsid,
                        "document_url": f"https://example.invalid/{dsid}", "sha256": period,
                        "retrieved_at": published, "first_seen_at": published,
                        "published_at": published, "status": "parsed"})


def test_a_month_missing_a_required_table_waits_and_names_it(tmp_path):
    db = _db(tmp_path)
    _dataset(db, "cba_loans_by_institution", "2026-07-31", "2026-08-26")
    res = R.evaluate_monthly(db, MONTHLY_RULES, TODAY)

    assert res.state == "waiting" and not res.ok
    assert res.missing == ["cba_deposits"]
    assert any(r["rule"] == "required_datasets" and not r["met"] for r in res.reasons)
    db.close()


def test_a_late_companion_holds_the_edition_only_for_the_grace_period(tmp_path):
    db = _db(tmp_path)
    for dsid in ("cba_loans_by_institution", "cba_deposits"):
        _dataset(db, dsid, "2026-07-31", "2026-09-17")        # released two days ago
    res = R.evaluate_monthly(db, MONTHLY_RULES, TODAY)
    assert res.state == "waiting" and res.missing == ["cba_bank_pnl"]

    # ten days after the release the wait is over: publish, and say what is missing
    for dsid in ("cba_loans_by_institution", "cba_deposits"):
        _dataset(db, dsid, "2026-07-31", "2026-09-05")
    res = R.evaluate_monthly(db, MONTHLY_RULES, TODAY)
    assert res.state == "partial" and res.ok and res.missing == ["cba_bank_pnl"]
    db.close()


def test_staleness_is_measured_from_the_release_not_the_period_it_describes(tmp_path):
    """The July tables appear in late August. Judging by period end would call a healthy series
    stale every month in the days before its next release."""
    db = _db(tmp_path)
    for dsid in ("cba_loans_by_institution", "cba_deposits", "cba_bank_pnl"):
        _dataset(db, dsid, "2026-07-31", "2026-08-26")        # period 50 days old, release 24 days old
    res = R.evaluate_monthly(db, MONTHLY_RULES, TODAY)
    assert res.state == "ready"

    # the publisher then goes quiet for two months: that is stale, and is not published over
    later = dt.date(2026, 11, 1)
    res = R.evaluate_monthly(db, MONTHLY_RULES, later)
    assert res.state == "stale" and not res.ok
    assert "have not been republished" in res.detail["note"]
    db.close()


def test_a_critical_quality_failure_blocks_a_report_that_requires_a_clean_bill(tmp_path):
    db = _db(tmp_path)
    for dsid in ("cba_loans_by_institution", "cba_deposits", "cba_bank_pnl"):
        _dataset(db, dsid, "2026-07-31", "2026-08-26")
    assert R.evaluate_monthly(db, MONTHLY_RULES, TODAY).state == "ready"

    gate = R.quality_gate("monthly", MONTHLY_RULES, {"summary": {"critical": 2}})
    assert gate is not None and gate.state == "blocked" and not gate.ok
    # a warning-only report is not blocked
    assert R.quality_gate("monthly", MONTHLY_RULES, {"summary": {"critical": 0, "warning": 5}}) is None
    # nor is a report that does not ask for the gate
    assert R.quality_gate("mpr_brief", {"require_quality_pass": False}, {"summary": {"critical": 2}}) is None
    db.close()


def _publication(db, pub_id: str, pub_type: str, released: str, passages: int, status: str = "verified") -> None:
    db.upsert_publication({"publication_id": pub_id, "source_id": "CBA", "pub_type": pub_type,
                           "edition_key": released, "published_at": released, "original_language": "az"})
    db.replace_passages(pub_id + ":doc", [
        {"passage_id": f"{pub_id}#{i}", "doc_id": pub_id + ":doc", "publication_id": pub_id, "language": "az",
         "page_index": i, "printed_page": str(i), "section": None, "kind": "text", "ord": i,
         "text": f"sentence {i}", "extraction_method": "pdf_text", "validation_status": status}
        for i in range(passages)])


def test_a_backfilled_archive_is_not_a_release(tmp_path):
    """Loading years of editions in one run is the case this rule exists for."""
    db = _db(tmp_path)
    rules = {"release_within_days": 45, "min_passages": 10, "require_extraction": "verified"}
    _publication(db, "mpr:2021-03", "monetary_policy_review", "2021-04-15", passages=60)
    res = R.evaluate_publication(db, "mpr_brief", rules, dict(db.get_publication("mpr:2021-03")), TODAY)

    assert res.state == "waiting" and not res.ok
    assert "archive rather than a release" in res.detail["note"]
    assert res.detail["age_days"] > 45
    db.close()


def test_a_recent_publication_that_did_not_extract_is_not_briefed(tmp_path):
    """A scanned or placeholder upload downloads fine and says nothing. Briefing from it would
    quote text nothing has confirmed."""
    db = _db(tmp_path)
    rules = {"release_within_days": 45, "min_passages": 40, "require_extraction": "verified"}
    _publication(db, "mpr:2026-08", "monetary_policy_review", "2026-08-26", passages=4)
    res = R.evaluate_publication(db, "mpr_brief", rules, dict(db.get_publication("mpr:2026-08")), TODAY)
    assert res.state == "waiting" and "too little text" in res.detail["note"]

    # enough text, but none of it verified
    _publication(db, "mpr:2026-08b", "monetary_policy_review", "2026-08-26", passages=60, status="needs_ocr")
    res = R.evaluate_publication(db, "mpr_brief", rules, dict(db.get_publication("mpr:2026-08b")), TODAY)
    assert res.state == "waiting" and "not verified" in res.detail["note"]

    # recent, extracted and verified: ready
    _publication(db, "mpr:2026-08c", "monetary_policy_review", "2026-08-26", passages=60)
    res = R.evaluate_publication(db, "mpr_brief", rules, dict(db.get_publication("mpr:2026-08c")), TODAY)
    assert res.state == "ready" and res.ok
    db.close()


def test_a_publication_without_a_release_date_is_never_called_new(tmp_path):
    db = _db(tmp_path)
    db.upsert_publication({"publication_id": "mpr:unknown", "source_id": "CBA",
                           "pub_type": "monetary_policy_review", "edition_key": "unknown"})
    res = R.evaluate_publication(db, "mpr_brief", {"release_within_days": 45},
                                 dict(db.get_publication("mpr:unknown")), TODAY)
    assert res.state == "blocked" and not res.ok
    db.close()


def test_a_decision_without_a_parsed_rate_is_not_briefed(tmp_path):
    db = _db(tmp_path)
    rules = {"release_within_days": 10, "min_passages": 3, "require_extraction": "verified",
             "require_decision_fields": ["policy_rate"]}
    _publication(db, "dec:2026-09-15", "policy_decision", "2026-09-15", passages=6)
    res = R.evaluate_publication(db, "decision_update", rules,
                                 dict(db.get_publication("dec:2026-09-15")), TODAY)
    assert res.state == "waiting" and "no policy_rate" in res.detail["note"]

    db.upsert_decision({"decision_id": "decision:2026-09-15", "publication_id": "dec:2026-09-15",
                        "announcement_date": "2026-09-15", "policy_rate": 6.5})
    res = R.evaluate_publication(db, "decision_update", rules,
                                 dict(db.get_publication("dec:2026-09-15")), TODAY)
    assert res.state == "ready"
    db.close()


def test_a_quiet_source_is_reported_as_stale_with_its_own_cadence(tmp_path):
    """A half-yearly report and a monthly table cannot share one staleness limit."""
    db = _db(tmp_path)
    db.set_dataset_state("cba_deposits", source_id="CBA", last_checked_at="2026-09-19T00:00:00+00:00",
                         last_changed_at="2026-06-01T00:00:00+00:00", latest_period_end="2026-05-31")
    db.set_dataset_state("cba_financial_stability_report", source_id="CBA",
                         last_checked_at="2026-09-19T00:00:00+00:00",
                         last_changed_at="2026-05-01T00:00:00+00:00", latest_period_end="2025-12-31")
    rules = {"stale_after_days": {"default": 45, "cba_financial_stability_report": 220}}

    stale = {s["dataset_id"] for s in R.stale_sources(db, rules, TODAY)}
    assert "cba_deposits" in stale                       # monthly table quiet for 110 days
    assert "cba_financial_stability_report" not in stale  # half-yearly, 141 days is normal
    db.close()
