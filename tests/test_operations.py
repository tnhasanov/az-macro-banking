"""Publication decisions, scheduling and unattended operation."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from azmonitor.parsers.base import Observation
from azmonitor.scheduling.fingerprint import describe_change, edition_inputs
from azmonitor.scheduling.run_due import JobLock, _publication_briefs
from azmonitor.storage.backup import backup_database, integrity_ok, restore_database, verify_dataset
from azmonitor.storage.db import Database


def _fp(metrics: dict, publications: dict | None = None, anchors: dict | None = None) -> dict:
    return {
        "fact_pack_hash": "h",
        "anchors": anchors or {"banking": {"period_end": "2026-07-31"}, "macro": {"period_end": "2026-08-31"}},
        "edition": {"edition_month": "2026-07", "banking_period": "2026-07-31"},
        "metrics": metrics,
        "availability": {"missing": []},
        "quality": {"checks": []},
        "publications": {"all": [{"publication_id": k, "published_at": v} for k, v in (publications or {}).items()]},
    }


def _metric(value: float, period: str = "2026-07-31") -> dict:
    return {"id": "cba.loans.total_ci.yoy", "latest": {"period": period, "value": value}, "unit": "%"}


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.sqlite")
    yield database
    database.close()


# ------------------------------------------------------- what triggers an edition

def test_identical_inputs_produce_an_identical_fingerprint(db):
    a = edition_inputs(_fp({"m": _metric(12.88)}), db, None, "facts_only")
    b = edition_inputs(_fp({"m": _metric(12.88)}), db, None, "facts_only")
    assert a == b
    assert describe_change({"inputs": a}, {"inputs": b})["details"] == []


def test_a_revision_to_an_unchanged_period_is_a_change(db):
    """The banking month has not moved, but a displayed value has: that is a new edition."""
    before = {"inputs": edition_inputs(_fp({"m": _metric(12.88)}), db, None, "facts_only")}
    after = {"inputs": edition_inputs(_fp({"m": _metric(12.41)}), db, None, "facts_only")}
    change = describe_change(before, after)
    assert any("revised for an unchanged period" in d for d in change["details"])


def test_a_new_publication_triggers_an_edition_with_an_unchanged_banking_anchor(db):
    before = {"inputs": edition_inputs(_fp({"m": _metric(12.88)}, {"policy_decision:1": "2026-06-24"}),
                                       db, None, "facts_only")}
    after = {"inputs": edition_inputs(_fp({"m": _metric(12.88)},
                                          {"policy_decision:1": "2026-06-24", "policy_decision:2": "2026-07-31"}),
                                      db, None, "facts_only")}
    change = describe_change(before, after)
    assert before["inputs"]["anchors"] == after["inputs"]["anchors"]       # the banking month did not move
    assert any("new publication" in d for d in change["details"])


def test_a_policy_decision_that_changes_only_its_rationale_is_a_change(db):
    base = {"decision_id": "decision:2026-07-31", "announcement_date": "2026-07-31", "policy_rate": 6.5,
            "corridor_floor": 5.5, "corridor_ceiling": 7.5, "action": "hold", "rationale_text": "first wording",
            "rationale_language": "en"}
    assert db.upsert_decision(base) == "new"
    before = {"inputs": edition_inputs(_fp({"m": _metric(12.88)}), db, None, "facts_only")}
    assert db.upsert_decision({**base, "rationale_text": "a different assessment of the risks",
                               "next_decision_date": "2026-09-23"}) == "revised"
    after = {"inputs": edition_inputs(_fp({"m": _metric(12.88)}), db, None, "facts_only")}
    assert any("policy decision detail changed" in d for d in describe_change(before, after)["details"])


def test_a_translated_decision_is_not_a_revision(db):
    az = {"decision_id": "decision:2026-07-31", "announcement_date": "2026-07-31", "policy_rate": 6.5,
          "corridor_floor": 5.5, "corridor_ceiling": 7.5, "action": "hold",
          "rationale_text": "Azərbaycan dilində mətn", "rationale_language": "az"}
    assert db.upsert_decision(az) == "new"
    en = {**az, "rationale_text": "the same decision in English", "rationale_language": "en"}
    assert db.upsert_decision(en) == "translated"
    assert db.upsert_decision(en) == "unchanged"
    rows = db.conn.execute("SELECT COUNT(*) FROM policy_decisions WHERE status='current'").fetchone()[0]
    assert rows == 1


def test_a_changed_narrative_file_is_a_change(tmp_path, db):
    path = tmp_path / "n.json"
    path.write_text(json.dumps({"cover": {"headline": "first"}}), encoding="utf-8")
    before = {"inputs": edition_inputs(_fp({"m": _metric(12.88)}), db, str(path), "analyst_file")}
    path.write_text(json.dumps({"cover": {"headline": "revised"}}), encoding="utf-8")
    after = {"inputs": edition_inputs(_fp({"m": _metric(12.88)}), db, str(path), "analyst_file")}
    assert any("narrative changed" in d for d in describe_change(before, after)["details"])


def test_a_critical_quality_failure_is_carried_into_the_fingerprint(db):
    fp = _fp({"m": _metric(12.88)})
    fp["quality"] = {"checks": [{"id": "components_sum:x", "ok": False, "severity": "critical"}]}
    inputs = edition_inputs(fp, db, None, "facts_only")
    assert inputs["quality_blocking"] == ["components_sum:x"]


# --------------------------------------------------------------- briefing policy

def _publication(db: Database, pub_id: str, pub_type: str, published_at: str) -> None:
    db.upsert_publication({"publication_id": pub_id, "source_id": "CBA", "pub_type": pub_type,
                           "edition_key": published_at, "edition_label": published_at,
                           "reporting_period_end": published_at, "published_at": published_at,
                           "published_at_basis": "test", "status": "extracted"})


def test_a_backfill_of_old_editions_does_not_produce_a_flood_of_briefs(db, monkeypatch):
    """Every archive edition is newly *stored*; none of them is newly *released*."""
    today = dt.date.today()
    _publication(db, "monetary_policy_review:2020-03", "monetary_policy_review", "2020-05-11")
    _publication(db, "monetary_policy_review:2021-06", "monetary_policy_review", "2021-08-10")
    _publication(db, "financial_stability_report:2022-A", "financial_stability_report", "2023-04-11")
    fresh_date = (today - dt.timedelta(days=3)).isoformat()
    _publication(db, "policy_decision:new", "policy_decision", fresh_date)
    state: dict = {}
    result = _publication_briefs(db, {"schedule": {"brief_within_days": 45}, "language": "en"}, state, dry_run=True)
    generated = {g["publication_id"] for g in result["generated"]}
    assert generated == {"policy_decision:new"}
    assert len(result["skipped_old"]) == 3


def test_a_publication_is_briefed_once(db):
    fresh = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _publication(db, "policy_decision:x", "policy_decision", fresh)
    state: dict = {}
    first = _publication_briefs(db, {"schedule": {}, "language": "en"}, state, dry_run=True)
    assert [g["publication_id"] for g in first["generated"]] == ["policy_decision:x"]
    state["briefed_publications"] = ["policy_decision:x"]
    second = _publication_briefs(db, {"schedule": {}, "language": "en"}, state, dry_run=True)
    assert second["generated"] == [] and second["already_briefed"] == ["policy_decision:x"]


def test_a_publication_without_a_release_date_is_not_briefed(db):
    db.upsert_publication({"publication_id": "monetary_policy_review:x", "source_id": "CBA",
                           "pub_type": "monetary_policy_review", "edition_key": "x", "edition_label": "x",
                           "published_at": None, "status": "extracted"})
    result = _publication_briefs(db, {"schedule": {}, "language": "en"}, {}, dry_run=True)
    assert result["generated"] == []
    assert result["skipped_old"][0]["reason"] == "no verified release date"


# ----------------------------------------------------------- unattended operation

def test_a_second_run_cannot_take_the_lock(tmp_path):
    lock = tmp_path / "run.lock"
    with JobLock(lock, 120):
        with pytest.raises(RuntimeError, match="another run holds the lock"):
            with JobLock(lock, 120):
                pass
    assert not lock.exists()


def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path):
    lock = tmp_path / "run.lock"
    lock.write_text(json.dumps({"pid": 2 ** 22, "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}))
    with JobLock(lock, 120):
        assert lock.exists()
    assert not lock.exists()


def test_backup_is_consistent_and_restore_is_verified(tmp_path):
    src = Database(tmp_path / "live.sqlite")
    src.store_observations("ds", "doc", [Observation(series_id="s", period_end=dt.date(2026, 7, 31), value=1.0)])
    src.close()
    manifest = backup_database(tmp_path / "live.sqlite", tmp_path / "backups")
    assert manifest["counts"]["observations"] == 1 and manifest["integrity"] == "ok"
    info = restore_database(tmp_path / "backups" / manifest["file"], tmp_path / "restored.sqlite")
    assert info["counts"]["observations"] == 1
    assert verify_dataset(tmp_path / "restored.sqlite", min_documents=0)["ok"] is True


def test_a_corrupt_backup_is_refused_and_the_previous_file_survives(tmp_path):
    good = Database(tmp_path / "live.sqlite")
    good.store_observations("ds", "doc", [Observation(series_id="s", period_end=dt.date(2026, 7, 31), value=1.0)])
    good.close()
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"this is not a database")
    assert integrity_ok(corrupt)[0] is False
    with pytest.raises(RuntimeError, match="integrity check"):
        restore_database(corrupt, tmp_path / "live.sqlite")
    assert verify_dataset(tmp_path / "live.sqlite", min_documents=0)["ok"] is True


def test_a_restore_that_lands_an_empty_dataset_fails_verification(tmp_path):
    empty = Database(tmp_path / "empty.sqlite")
    empty.close()
    result = verify_dataset(tmp_path / "empty.sqlite", min_documents=50)
    assert result["ok"] is False
    assert "documents" in result["problems"][0]


def test_a_failed_run_leaves_the_previous_state_file_alone(tmp_path):
    from azmonitor.scheduling.run_due import _write_state

    state_path = tmp_path / "state.json"
    state: dict = {}
    _write_state(_Paths(tmp_path), state_path, state, {"status": "ok", "finished_at": "t1"})
    assert json.loads(state_path.read_text())["last_successful_run"]["at"] == "t1"
    _write_state(_Paths(tmp_path), state_path, state, {"status": "failed", "finished_at": "t2"})
    written = json.loads(state_path.read_text())
    assert written["last_successful_run"]["at"] == "t1"      # the failure did not overwrite it
    assert written["last_run"]["status"] == "failed"


class _Paths:
    def __init__(self, root: Path):
        self.state_dir = root
        self.logs_dir = root


# ------------------------------------------------------- quality gates on publishing

def test_a_critical_quality_failure_blocks_a_new_edition():
    """A failure on a period the edition displays stops publication; the last edition stays current."""
    from azmonitor.reports import blocking_quality_failures

    fp = _fp({"m": _metric(12.88)})
    fp["quality"] = {"checks": [
        {"id": "components_sum:cba_deposits", "ok": False, "severity": "critical", "failed_periods": ["2026-07-31"]},
        {"id": "components_sum:cba_loans", "ok": False, "severity": "warning", "failed_periods": ["2015-12-31"]},
        {"id": "freshness", "ok": True, "severity": "info"},
    ]}
    blocking = blocking_quality_failures(fp, {})
    assert [c["id"] for c in blocking] == ["components_sum:cba_deposits"]
    # the deliberate override is explicit, off by default, and covers only the operator who sets it
    assert blocking_quality_failures(fp, {"quality": {"allow_publication_with_critical_failures": True}}) == []


def test_severity_depends_on_whether_the_failure_touches_the_displayed_window():
    from azmonitor.calc.validate import classify

    old = {"id": "components_sum:x", "ok": False, "failed": 1, "n": 100,
           "examples": [{"period_end": "2015-12-31", "diff": 31.7}]}
    recent = {"id": "components_sum:x", "ok": False, "failed": 1, "n": 100,
              "examples": [{"period_end": "2026-07-31", "diff": 31.7}]}
    window = dt.date(2023, 8, 1)
    assert classify(old, window, {})["severity"] == "warning"
    assert classify(recent, window, {})["severity"] == "critical"


def test_an_exception_must_name_every_failing_period():
    from azmonitor.calc.validate import classify

    check = {"id": "components_sum:x", "ok": False, "failed": 2, "n": 100,
             "examples": [{"period_end": "2026-07-31"}, {"period_end": "2026-06-30"}]}
    partial = {"components_sum:x": {"periods": ["2026-07-31"], "reason": "known source issue",
                                    "decided_by": "CRO", "review_by": "2027-01-31"}}
    full = {"components_sum:x": {"periods": ["2026-07-31", "2026-06-30"], "reason": "known source issue",
                                 "decided_by": "CRO", "review_by": "2027-01-31"}}
    assert classify(dict(check), dt.date(2023, 8, 1), partial)["severity"] == "critical"
    accepted = classify(dict(check), dt.date(2023, 8, 1), full)
    assert accepted["severity"] == "accepted_exception" and accepted["ok"] is True
    assert accepted["exception"]["decided_by"] == "CRO"


def test_a_run_without_the_analyst_narrative_says_so_rather_than_blaming_the_file():
    """A scheduled run that falls back to facts-only text produces a different deck, so it is a
    change - but the reason is the source, not an edit to a file nobody touched."""
    from azmonitor.scheduling.fingerprint import describe_change

    with_file = {"inputs": {"narrative": "file:abc123"}}
    facts_only = {"inputs": {"narrative": "mode:facts_only"}}

    assert describe_change(with_file, facts_only)["details"] == [
        "the narrative source changed from an analyst narrative to facts_only text"]
    assert describe_change(facts_only, with_file)["details"] == [
        "the narrative source changed from facts_only text to an analyst narrative"]
    # an edited analyst file is still reported as exactly that
    assert describe_change(with_file, {"inputs": {"narrative": "file:def456"}})["details"] == [
        "the supplied narrative changed"]
    # and an unchanged narrative is not a reason for a new edition
    assert describe_change(with_file, with_file)["trigger"] == "no material change"
