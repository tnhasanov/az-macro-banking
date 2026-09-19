"""The whole workflow, from a release appearing to a message being recorded.

Three runs matter, and they are the three this exercises:

1. **A controlled sample release.** A publication appears, readiness passes, a brief is produced and
   a delivery is recorded.
2. **The same run again.** Nothing has changed, so nothing is produced and nothing is sent. This is
   the run that happens three times a day, every day, and getting it wrong means either a flood of
   duplicate reports or a system that stops noticing.
3. **A failed validation.** A critical data-quality failure inside the displayed window blocks the
   edition, the previous one stays current, and an alert says why.

The sources are never contacted. A controlled fixture is the only way to test a release, because a
real one happens when the Central Bank decides and not when a test runs.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from azmonitor.delivery.records import DeliveryLedger
from azmonitor.scheduling import readiness as R
from azmonitor.scheduling import tasks as T
from azmonitor.storage.db import Database

TODAY = dt.date(2026, 9, 19)


@pytest.fixture()
def workspace(tmp_data_env):
    """An isolated data and output directory, so nothing touches the real dataset."""
    from azmonitor import config

    config.paths().ensure()
    return tmp_data_env


def _release(db: Database, pub_id: str, pub_type: str, released: str, *, passages: int = 90,
             translated: str | None = None) -> None:
    """A publication as the collector would have stored it, without contacting anything."""
    db.upsert_publication({"publication_id": pub_id, "source_id": "CBA", "pub_type": pub_type,
                           "edition_key": released[:7], "edition_label": released,
                           "published_at": released, "published_at_basis": "date on the publication page",
                           "original_language": "az",
                           **({"translation_available_at": translated} if translated else {})})
    db.replace_passages(f"{pub_id}:doc", [
        {"passage_id": f"{pub_id}#{i}", "doc_id": f"{pub_id}:doc", "publication_id": pub_id, "language": "az",
         "page_index": i // 10, "printed_page": str(i // 10), "section": None, "kind": "text", "ord": i,
         "text": f"Sentence {i} of the publication.", "extraction_method": "pdf_text",
         "validation_status": "verified"} for i in range(passages)])


# ------------------------------------------------------- 1. a controlled release

def test_a_controlled_release_becomes_a_brief_and_a_delivery(workspace, monkeypatch):
    from azmonitor import config
    from azmonitor.delivery import dispatch

    db = Database(config.paths().db_path)
    _release(db, "monetary_policy_review:2026-08", "monetary_policy_review", "2026-09-05")

    rules = (config.schedule_config()["releases"]["mpr_brief"])["readiness"]
    res = R.evaluate_publication(db, "mpr_brief", rules,
                                 dict(db.get_publication("monetary_policy_review:2026-08")), TODAY)
    assert res.state == "ready", res.as_dict()
    assert res.detail["age_days"] == 14        # released a fortnight ago: news, not archive

    # the delivery the run would record, with no provider contacted
    monkeypatch.setenv("AZMONITOR_OWNER_EMAIL", "owner@example.invalid")
    ledger = DeliveryLedger(workspace / "data" / "deliveries.sqlite")
    did, state = ledger.enqueue(report_type="mpr_brief", edition="2026-08", version=1, channel="email",
                                recipient_id="owner", recipient_address="owner@example.invalid",
                                provider="microsoft_graph", subject="CBA MPR 2026-08 - brief",
                                content_hash="h1")
    assert state == "new"
    ledger.mark_sending(did)
    ledger.mark_sent(did, "graph-accepted-202")
    assert ledger.summary() == {"sent": 1}
    ledger.close()
    db.close()


# ------------------------------------------------------------ 2. unchanged rerun

def test_the_same_run_again_produces_nothing_and_sends_nothing(workspace, monkeypatch):
    """The run that happens three times a day. It must be able to do nothing, quietly."""
    from azmonitor import config

    db = Database(config.paths().db_path)
    _release(db, "monetary_policy_review:2026-08", "monetary_policy_review", "2026-09-05")

    # the brief has already been produced and recorded in the run state
    state = {"briefed_publications": ["monetary_policy_review:2026-08"]}
    from azmonitor.scheduling.run_due import _publication_briefs

    out = _publication_briefs(db, {"schedule": {"brief_within_days": 45}}, state, dry_run=False)
    assert out["generated"] == []
    assert "monetary_policy_review:2026-08" in out["already_briefed"]

    # and the delivery ledger refuses a second send of the same edition to the same person
    ledger = DeliveryLedger(workspace / "data" / "deliveries.sqlite")
    kw = dict(report_type="mpr_brief", edition="2026-08", version=1, channel="email",
              recipient_id="owner", recipient_address="owner@example.invalid",
              provider="microsoft_graph", subject="s", content_hash="h1")
    did, _ = ledger.enqueue(**kw)
    ledger.mark_sending(did)
    ledger.mark_sent(did, "m1")
    assert ledger.enqueue(**kw)[1] == "sent"
    assert ledger.summary() == {"sent": 1}       # still one, not two
    ledger.close()
    db.close()


# --------------------------------------------------------- 3. failed validation

def test_a_failed_validation_blocks_the_edition_and_explains_itself(workspace):
    """A critical failure inside the displayed window stops publication. The previous edition stays
    current, which is the point: a board pack with a known-wrong figure is worse than a late one."""
    from azmonitor import config

    rules = config.schedule_config()["releases"]["monthly"]["readiness"]
    gate = R.quality_gate("monthly", rules, {"summary": {"critical": 1, "warning": 0}})

    assert gate is not None and gate.state == "blocked" and not gate.ok
    assert "previous edition stays current" in gate.detail["note"]
    failed_rule = next(r for r in gate.reasons if r["rule"] == "require_quality_pass")
    assert failed_rule["met"] is False and "1 critical" in failed_rule["saw"]

    # the same failures outside the window are warnings and stop nothing
    assert R.quality_gate("monthly", rules, {"summary": {"critical": 0, "warning": 5}}) is None


def test_a_backfill_of_the_archive_triggers_nothing(workspace):
    """The run after a backfill. Ninety publications arrive at once and none of them is news."""
    from azmonitor import config
    from azmonitor.scheduling.run_due import _publication_briefs

    db = Database(config.paths().db_path)
    for year in range(2020, 2026):
        _release(db, f"monetary_policy_review:{year}-03", "monetary_policy_review", f"{year}-04-15")
    # plus one that really is recent
    _release(db, "monetary_policy_review:2026-08", "monetary_policy_review", "2026-09-05")

    state: dict = {}
    out = _publication_briefs(db, {"schedule": {"brief_within_days": 45}}, state, dry_run=True)
    would = [g["publication_id"] for g in out["generated"]]

    assert would == ["monetary_policy_review:2026-08"]
    assert len(out["skipped_old"]) == 6
    db.close()


# ------------------------------------------------------------- the weekly window

def test_the_weekly_digest_covers_the_week_that_just_ended(workspace):
    monday = dt.datetime(2026, 9, 21, 8, 30, tzinfo=T.tz())
    start, end = T.previous_calendar_week(monday)
    assert (start.isoformat(), end.isoformat()) == ("2026-09-14", "2026-09-20")
    assert start.weekday() == 0 and end.weekday() == 6

    # the boundaries do not move with the day the digest is actually produced
    wednesday = dt.datetime(2026, 9, 23, 17, 0, tzinfo=T.tz())
    assert T.previous_calendar_week(wednesday) == (start, end)

    lo, hi = T.week_bounds_utc(start, end)
    assert lo.startswith("2026-09-13T20:00")        # Baku is UTC+4, so the week starts the evening before
    assert hi.startswith("2026-09-20T19:59")


def test_a_missed_run_is_only_missed_once_something_has_ever_run(workspace):
    """A fresh install has not missed anything; a host that was off for a day has missed several."""
    assert T.missed_runs() == []                     # no history at all

    hist = {"runs": [{"task": "source-check", "status": "ok",
                      "at_utc": "2026-09-14T05:15:00+00:00", "at_local": "2026-09-14T09:15:00+04:00"}]}
    (workspace / "data" / "state").mkdir(parents=True, exist_ok=True)
    (workspace / "data" / "state" / "run_history.json").write_text(json.dumps(hist), encoding="utf-8")

    now = dt.datetime(2026, 9, 15, 20, 0, tzinfo=T.tz())
    missed = T.missed_runs(now)
    # the 13:15 and 17:15 on the 14th, and all three on the 15th
    assert [m["due_local"][5:16] for m in missed] == ["09-14T13:15", "09-14T17:15",
                                                      "09-15T09:15", "09-15T13:15", "09-15T17:15"]
    assert all(m["task"] == "source-check" for m in missed)
    assert all(m["due_local"].endswith("+04:00") for m in missed)   # reported in Asia/Baku


# --------------------------------------------------------------- report archive

def test_retention_keeps_the_latest_and_anything_delivered(workspace):
    """A recipient holding a PDF and finding the archive has forgotten it is worse than a big disk."""
    from azmonitor import config
    from azmonitor.delivery.records import DeliveryLedger
    from azmonitor.scheduling import archive

    out = config.paths().output_dir
    edition = out / "monthly" / "2026-07"
    # v9 sorts after v36 as a string: the ordering this exercises is numeric
    for n in (1, 2, 9, 12, 36):
        d = edition / f"v{n}_20260919T00000{n}Z"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"version": n}), encoding="utf-8")
        (d / "deck.pdf").write_bytes(b"x" * 1024)
    (out / "latest").mkdir(parents=True, exist_ok=True)
    (out / "latest" / "monthly").symlink_to(edition / "v36_20260919T000036Z")

    assert archive._version_number(edition / "v36_x") == 36
    assert [archive._version_number(p) for p in archive._versions(edition)] == [1, 2, 9, 12, 36]

    # v2 was delivered to someone and must survive a retention limit of two
    led = DeliveryLedger(config.paths().data_dir / "deliveries.sqlite")
    did, _ = led.enqueue(report_type="monthly", edition="2026-07", version=2, channel="email",
                         recipient_id="owner", recipient_address="o@example.invalid",
                         provider="microsoft_graph", subject="s", content_hash="h")
    led.mark_sending(did)
    led.mark_sent(did, "m1")
    led.close()

    import azmonitor.config as C
    original = C.schedule_config
    C.schedule_config = lambda: {**original(), "archive": {"keep_versions_per_edition": 2, "keep_editions": {}}}
    try:
        p = archive.plan()
        removed = {r["version"] for r in p["would_remove"]}
        kept = {k["reason"] for k in p["kept_despite_limits"]}
        assert removed == {1, 9}                       # v2 delivered, v12 and v36 within the limit
        assert "this version was delivered to a recipient" in kept
        assert p["applied"] is False if "applied" in p else True
    finally:
        C.schedule_config = original
