"""Application state: the guarantees the constraints and state transitions are there to provide.

Run against a real Postgres (see appstate_fixtures.py), because every property worth testing here
is a property of the database — a partial unique index, a row lock, a fence compared inside an
UPDATE — and a mock would test the mock.
"""
from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import pytest

from azmonitor.appstate import JOB_ID_PATTERN, MIGRATIONS, export_typescript, migrate, new_id, schema_version
from azmonitor.appstate import jobs as J
from azmonitor.appstate import publication as P

from appstate_fixtures import add_recipient, pg, settings  # noqa: F401 - pytest fixture

WEB_SCHEMA = Path(__file__).resolve().parents[1] / "web" / "lib" / "generated" / "appstate-schema.ts"


def _job(conn, **kw):
    base = dict(kind="report", report_type="monthly", params={"period": "latest", "refresh": "latest_data"},
                trigger="manual", environment="production", requested_by="owner@example.az",
                requester_email="owner@example.az")
    base.update(kw)
    return J.create_job(conn, **base)


# ------------------------------------------------------------------ the schema itself

def test_the_web_application_applies_exactly_these_migrations():
    """Both sides migrate; if their lists drift, whichever deploys first decides the schema."""
    assert WEB_SCHEMA.read_text(encoding="utf-8") == export_typescript(), \
        "run `python -m azmonitor.appstate export-ts` and commit the result"


def test_migrations_are_idempotent_and_recorded(pg):
    assert migrate(pg) == []
    assert schema_version(pg) == max(v for v, _, _ in MIGRATIONS)


def test_ids_sort_in_creation_order_and_match_the_pattern():
    import re
    import time

    a = new_id("job")
    time.sleep(0.002)
    b = new_id("job")
    assert a < b and re.match(JOB_ID_PATTERN, a) and re.match(JOB_ID_PATTERN, b)


# ------------------------------------------------------------------ requests

def test_an_equivalent_request_returns_the_active_job(pg):
    first, created = _job(pg)
    second, created_again = _job(pg)
    assert created and not created_again and first == second


def test_parameter_order_does_not_make_two_requests_different(pg):
    """The key is computed from jsonb, which Postgres normalises; a dict built in another order in
    another language is the same request."""
    first, _ = _job(pg, params={"period": "latest", "refresh": "latest_data"})
    second, created = _job(pg, params={"refresh": "latest_data", "period": "latest"})
    assert first == second and not created


def test_a_double_click_from_two_connections_creates_one_job(pg):
    """The race a read-then-insert check loses: both requests arrive before either has committed."""
    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(6)

    def click():
        conn = pg.connect_again()
        barrier.wait()
        results.append(_job(conn))
        conn.close()

    threads = [threading.Thread(target=click) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({job_id for job_id, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    assert pg.execute("SELECT count(*) FROM job_dispatches").fetchone()[0] == 1


def test_a_finished_job_does_not_block_a_new_request(pg):
    first, _ = _job(pg)
    claim = J.claim(pg, first, worker_id="w1")
    J.finish(pg, claim, "reused", edition_id="monthly:2026-07:v1")
    second, created = _job(pg)
    assert created and second != first


def test_a_scheduled_slot_produces_one_job_however_often_the_scheduler_fires(pg):
    slot = "source-check@2026-09-26T09:15+04:00"
    ids = {_job(pg, kind="source_check", report_type=None, params={}, trigger="schedule",
                schedule_slot=slot, requested_by="scheduler")[0] for _ in range(3)}
    assert len(ids) == 1


def test_every_job_is_born_with_a_dispatch_to_perform(pg):
    """The outbox row is written with the job, so there is no moment when a job exists that nothing
    will ever start."""
    job_id, _ = _job(pg)
    rows = pg.execute("SELECT status, attempt FROM job_dispatches WHERE job_id = %s", (job_id,)).fetchall()
    assert rows == [("pending", 1)]


# ------------------------------------------------------------------ ownership

def test_a_second_claim_of_a_running_job_gets_nothing(pg):
    job_id, _ = _job(pg)
    assert J.claim(pg, job_id, worker_id="w1") is not None
    assert J.claim(pg, job_id, worker_id="w2") is None, "a duplicate dispatch must be a no-op"


def test_a_lapsed_lease_can_be_taken_over_and_the_old_worker_is_fenced_out(pg):
    job_id, _ = _job(pg)
    old = J.claim(pg, job_id, worker_id="w1", lease_seconds=1)
    pg.execute("UPDATE report_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s", (job_id,))
    new = J.claim(pg, job_id, worker_id="w2")
    assert new is not None and new.fence > old.fence and new.attempt == old.attempt + 1

    with pytest.raises(J.LeaseLost):
        J.set_stage(pg, old, "rendering")
    with pytest.raises(J.LeaseLost):
        J.finish(pg, old, "succeeded")
    assert not J.heartbeat(pg, job_id, old.fence), "the old worker's heartbeat must report the loss"
    J.set_stage(pg, new, "rendering")                                   # the new one carries on


def test_a_lease_kept_alive_by_the_heartbeat_thread_does_not_lapse(pg):
    job_id, _ = _job(pg)
    claim = J.claim(pg, job_id, worker_id="w1", lease_seconds=2)
    hb = J.Heartbeat(pg.connect_again, interval=0.3, lease_seconds=2).start()
    hb.add(claim)
    try:
        import time
        time.sleep(2.5)
        expires = pg.execute("SELECT lease_expires_at > now() FROM report_jobs WHERE job_id = %s",
                             (job_id,)).fetchone()[0]
        assert expires and hb.beats >= 3 and not claim.lost.is_set()
    finally:
        hb.stop()


def test_the_heartbeat_thread_notices_a_takeover(pg):
    job_id, _ = _job(pg)
    claim = J.claim(pg, job_id, worker_id="w1", lease_seconds=60)
    hb = J.Heartbeat(pg.connect_again, interval=0.2, lease_seconds=60).start()
    hb.add(claim)
    try:
        pg.execute("UPDATE report_jobs SET fence = fence + 1 WHERE job_id = %s", (job_id,))
        import time
        time.sleep(0.8)
        assert claim.lost.is_set()
        with pytest.raises(J.LeaseLost):
            claim.check("publishing")
    finally:
        hb.stop()


def test_a_cancelled_job_cannot_be_claimed(pg):
    job_id, _ = _job(pg)
    pg.execute("UPDATE report_jobs SET cancel_requested = true WHERE job_id = %s", (job_id,))
    assert J.claim(pg, job_id, worker_id="w1") is None


# ------------------------------------------------------------------ recovery

def test_a_worker_that_stops_responding_is_retried_then_failed(pg):
    job_id, _ = _job(pg)
    for attempt in (1, 2):
        c = J.claim(pg, job_id, worker_id=f"w{attempt}")
        assert c is not None and c.attempt == attempt
        pg.execute("UPDATE report_jobs SET lease_expires_at = now() - interval '1 second', "
                   "next_attempt_at = NULL WHERE job_id = %s", (job_id,))
        assert {"job_id": job_id, "action": "requeued"} in J.reap(pg, "production")
        row = J.get_job(pg, job_id)
        assert row["status"] == "queued" and row["next_attempt_at"] is not None
    c = J.claim(pg, job_id, worker_id="w3")
    pg.execute("UPDATE report_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s", (job_id,))
    assert {"job_id": job_id, "action": "failed"} in J.reap(pg, "production")
    row = J.get_job(pg, job_id)
    assert row["status"] == "failed" and row["error_code"] == "worker_lost"
    assert row["error_message"], "a failed job says why, rather than staying 'running' for ever"


def test_a_requeued_job_gets_a_dispatch_after_its_backoff(pg):
    job_id, _ = _job(pg)
    J.claim(pg, job_id, worker_id="w1")
    pg.execute("UPDATE job_dispatches SET status = 'accepted' WHERE job_id = %s", (job_id,))
    pg.execute("UPDATE report_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s", (job_id,))
    J.reap(pg, "production")
    pending = pg.execute("SELECT attempt, next_try_at > now() FROM job_dispatches "
                         "WHERE job_id = %s AND status = 'pending'", (job_id,)).fetchall()
    assert pending == [(2, True)]


def test_a_dispatch_no_runner_ever_claimed_is_sent_again_then_given_up(pg):
    job_id, _ = _job(pg)
    for n in range(3):
        pg.execute("UPDATE job_dispatches SET status = 'accepted', accepted_at = now() - interval '2 hours' "
                   "WHERE job_id = %s AND status = 'pending'", (job_id,))
        pg.execute("UPDATE report_jobs SET status = 'dispatched' WHERE job_id = %s", (job_id,))
        actions = J.reap(pg, "production")
        expected = "redispatched" if n < 2 else "failed"
        assert {"job_id": job_id, "action": expected} in actions, actions
    row = J.get_job(pg, job_id)
    assert row["status"] == "failed" and row["error_code"] == "runner_never_started"


def test_a_queued_job_with_no_dispatch_in_flight_gets_one(pg):
    job_id, _ = _job(pg)
    pg.execute("DELETE FROM job_dispatches WHERE job_id = %s", (job_id,))
    assert {"job_id": job_id, "action": "dispatch_restored"} in J.reap(pg, "production")


def test_reaping_one_environment_leaves_another_alone(pg):
    job_id, _ = _job(pg, environment="preview")
    J.claim(pg, job_id, worker_id="w1")
    pg.execute("UPDATE report_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s", (job_id,))
    assert J.reap(pg, "production") == []
    assert J.get_job(pg, job_id)["status"] == "running"


def test_a_retryable_failure_requeues_with_backoff_and_releases_ownership(pg):
    job_id, _ = _job(pg)
    c = J.claim(pg, job_id, worker_id="w1")
    assert J.retry_later(pg, c, error_code="sources_unreachable", error_message="the CBA site timed out") == "requeued"
    row = J.get_job(pg, job_id)
    assert row["status"] == "queued" and row["fence"] > c.fence
    with pytest.raises(J.LeaseLost):
        J.set_stage(pg, c, "rendering")


# ------------------------------------------------------------------ publication

def _record(edition="2026-07", version=1, *, report_type="monthly", fingerprint="fp1"):
    edition_id = f"{report_type}:{edition}:v{version}"
    return {
        "edition_id": edition_id, "report_type": report_type, "edition": edition, "version": version,
        "fingerprint": fingerprint,
        "manifest": {"artifacts": [{"key": f"reports/{report_type}/{edition}/v{version}/x/report.pdf",
                                    "bytes": 10, "sha256": "ab" * 32, "verified_at": "2026-09-26T00:00:00Z"}]},
        "validation": {"blocking": [], "numbers_checked": 40},
        "reporting_periods": {"banking": "2026-07-31"}, "findings": ["Loans grew 1.2% m/m."],
    }


def _catalog(record):
    return {"generated_at": "2026-09-26T09:00:00+00:00", "files": record["manifest"]["artifacts"],
            "summary": record["findings"], "blob_prefix": "x"}


def _publish(pg, *, trigger="source_change", cause="new_data", environment="production", version=1,
             notify=False, edition="2026-07"):
    job_id, _ = _job(pg, trigger=trigger, environment=environment, notify_requester=notify,
                     params={"period": edition, "v": version})
    claim = J.claim(pg, job_id, worker_id="w1")
    rec = _record(edition, version)
    return job_id, P.commit(pg, claim, record=rec, catalog_entry=_catalog(rec), cause=cause)


def test_publication_and_its_announcements_commit_together(pg):
    settings(pg)
    add_recipient(pg, "owner@example.az")
    job_id, out = _publish(pg)
    job = J.get_job(pg, job_id)
    assert job["status"] == "succeeded" and job["edition_id"] == "monthly:2026-07:v1"
    rows = pg.execute("SELECT purpose, status FROM email_outbox").fetchall()
    assert rows == [("new_edition", "queued")]
    assert pg.execute("SELECT is_latest FROM editions").fetchone()[0] is True


def test_a_worker_that_lost_its_lease_cannot_publish(pg):
    settings(pg)
    add_recipient(pg, "owner@example.az")
    job_id, _ = _job(pg)
    c = J.claim(pg, job_id, worker_id="w1")
    pg.execute("UPDATE report_jobs SET fence = fence + 1 WHERE job_id = %s", (job_id,))
    rec = _record()
    with pytest.raises(J.LeaseLost):
        P.commit(pg, c, record=rec, catalog_entry=_catalog(rec), cause="new_data")
    assert pg.execute("SELECT count(*) FROM publication_records").fetchone()[0] == 0
    assert pg.execute("SELECT count(*) FROM email_outbox").fetchone()[0] == 0, \
        "nothing half-published: the whole transaction rolled back"


def test_a_crash_inside_the_transaction_leaves_nothing_published(pg, monkeypatch):
    settings(pg)
    add_recipient(pg, "owner@example.az")
    job_id, _ = _job(pg)
    c = J.claim(pg, job_id, worker_id="w1")
    rec = _record()

    real = P.announcement_recipients

    def boom(*a, **k):
        real(*a, **k)
        raise RuntimeError("process killed mid-transaction")

    monkeypatch.setattr(P, "announcement_recipients", boom)
    with pytest.raises(RuntimeError):
        P.commit(pg, c, record=rec, catalog_entry=_catalog(rec), cause="new_data")
    for table in ("publication_records", "email_outbox"):
        assert pg.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert J.get_job(pg, job_id)["status"] == "running"


def test_the_same_version_cannot_be_published_twice(pg):
    _publish(pg)
    job_id, _ = _job(pg, params={"another": True})
    c = J.claim(pg, job_id, worker_id="w2")
    rec = _record()
    with pytest.raises(P.PublicationConflict):
        P.commit(pg, c, record=rec, catalog_entry=_catalog(rec), cause="new_data")


def test_a_preview_deployment_records_but_never_queues_a_subscriber_email(pg):
    settings(pg, environment="preview")
    add_recipient(pg, "owner@example.az")
    _publish(pg, environment="preview")
    status, reason = pg.execute("SELECT status, status_reason FROM email_outbox").fetchone()
    assert status == "suppressed" and "preview" in reason


def test_automatic_email_off_is_recorded_as_suppressed_not_silently_skipped(pg):
    settings(pg, auto_email_enabled=False)
    add_recipient(pg, "owner@example.az")
    _publish(pg)
    assert pg.execute("SELECT status FROM email_outbox").fetchone()[0] == "suppressed"


def test_a_manual_request_notifies_the_requester_and_nobody_else(pg):
    settings(pg)
    add_recipient(pg, "board@example.az")
    _publish(pg, trigger="manual", cause="manual_request", notify=True)
    rows = pg.execute("SELECT purpose, to_address FROM email_outbox").fetchall()
    assert rows == [("manual_request", "owner@example.az")]


def test_a_requester_who_is_also_a_subscriber_gets_one_message(pg):
    settings(pg)
    add_recipient(pg, "owner@example.az")
    _publish(pg, notify=True)
    assert pg.execute("SELECT count(*) FROM email_outbox").fetchone()[0] == 1


def test_a_revision_is_announced_as_one_and_waits_out_the_settle_window(pg):
    settings(pg, revision_settle_minutes=90)
    add_recipient(pg, "owner@example.az")
    _publish(pg, version=1)
    pg.execute("UPDATE email_outbox SET status = 'accepted'")          # v1 went out
    _publish(pg, version=2)
    purpose, delayed = pg.execute("SELECT purpose, not_before > now() + interval '80 minutes' "
                                  "FROM email_outbox WHERE status = 'queued'").fetchone()
    assert purpose == "revised_edition" and delayed


def test_a_burst_of_revisions_becomes_one_message_about_the_latest(pg):
    settings(pg)
    add_recipient(pg, "owner@example.az")
    _publish(pg, version=1)
    pg.execute("UPDATE email_outbox SET status = 'accepted'")
    _publish(pg, version=2)
    _publish(pg, version=3)
    rows = pg.execute("SELECT e.status, p.version FROM email_outbox e JOIN publication_records p "
                      "USING (edition_id) WHERE e.purpose = 'revised_edition' ORDER BY p.version").fetchall()
    assert rows == [("cancelled", 2), ("queued", 3)]


def test_a_revision_before_the_first_notice_went_out_is_announced_as_new(pg):
    """A reader who never heard of v1 should not be told v2 'revises' something."""
    settings(pg)
    add_recipient(pg, "owner@example.az")
    _publish(pg, version=1)
    _publish(pg, version=2)
    rows = pg.execute("SELECT e.status, e.purpose, p.version FROM email_outbox e JOIN publication_records p "
                      "USING (edition_id) ORDER BY p.version").fetchall()
    assert rows == [("cancelled", "new_edition", 1), ("queued", "new_edition", 2)]


def test_an_edition_is_announced_to_a_recipient_once_ever(pg):
    """The guarantee that does not depend on the provider's 24-hour idempotency window."""
    settings(pg)
    rid = add_recipient(pg, "owner@example.az")
    _publish(pg)
    with pytest.raises(Exception):
        pg.execute("INSERT INTO email_outbox(delivery_id, environment, edition_id, recipient_id, to_address, "
                   "purpose, status, idempotency_key) VALUES ('d2','production','monthly:2026-07:v1',%s,"
                   "'owner@example.az','new_edition','queued','d2')", (rid,))


def test_a_sector_subscription_matches_only_its_sector(pg):
    settings(pg)
    add_recipient(pg, "construction@example.az", report_types=("sector",), sector="construction")
    add_recipient(pg, "agri@example.az", report_types=("sector",), sector="agriculture")
    job_id, _ = _job(pg, report_type="sector", params={"sector": "construction"}, trigger="source_change")
    c = J.claim(pg, job_id, worker_id="w1")
    rec = _record(report_type="sector", edition="construction_2026-07")
    rec["sector"] = "construction"
    P.commit(pg, c, record=rec, catalog_entry=_catalog(rec), cause="new_data")
    assert [r[0] for r in pg.execute("SELECT to_address FROM email_outbox").fetchall()] == ["construction@example.az"]


def test_an_unsubscribed_or_suppressed_address_is_never_queued(pg):
    settings(pg)
    add_recipient(pg, "gone@example.az")
    add_recipient(pg, "bounced@example.az")
    pg.execute("UPDATE recipients SET unsubscribed_at = now() WHERE email = 'gone@example.az'")
    pg.execute("UPDATE recipients SET suppressed_at = now(), suppressed_reason = 'hard bounce' "
               "WHERE email = 'bounced@example.az'")
    _publish(pg)
    rows = pg.execute("SELECT to_address, status FROM email_outbox").fetchall()
    assert rows == [("bounced@example.az", "suppressed")]


def test_reaping_a_dead_worker_releases_the_dataset_lease_it_held(pg):
    """Otherwise the next job waits up to half an hour for a lease nobody will renew."""
    from azmonitor.cloud import lock as L

    job_id, _ = J.create_job(pg, kind="report", report_type="monthly",
                             params={"period": "latest", "refresh": "latest_data"}, trigger="manual",
                             environment="production", requested_by="owner")
    claim = J.claim(pg, job_id, worker_id="w1", lease_seconds=60)
    holder = f"{job_id}.{claim.attempt}/w1"
    lease = L.DatabaseLease(pg.connect_again(), "azmonitor-run", holder=holder)
    lease.try_acquire()
    other = L.DatabaseLease(pg.connect_again(), "azmonitor-run", holder="someone-else")
    with pytest.raises(L.LeaseNotAcquired):
        other.try_acquire()
    pg.execute("UPDATE report_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s", (job_id,))
    assert {r["action"] for r in J.reap(pg, "production")} == {"requeued"}
    other.try_acquire()                              # free at once, not in thirty minutes
    with pytest.raises(L.LeaseLost):
        lease.check("saving the dataset")            # and the zombie cannot write
