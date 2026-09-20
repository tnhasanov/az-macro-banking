"""Ownership, and what happens when a worker loses it without noticing.

The workflow allows a job 60 minutes and the lease expires after 55. That five-minute gap is not
theoretical: a run that overshoots keeps working with a lease that has already lapsed, and the next
scheduled run is entitled to take it. Two workers then hold the same dataset, and whichever saves
last silently discards the other's work.

Renewal narrows that window but cannot close it — a process can always be paused for longer than
its remaining lease. So the lease carries a fence token, bumped on every acquisition, and a worker
proves ownership immediately before anything persistent. A worker that was replaced finds a token
it does not recognise and stops.

These run against a real Postgres, because the interesting behaviour is the database arbitrating
between two connections, which a fake cannot do.
"""
from __future__ import annotations

import os

import pytest

from azmonitor.cloud import lock as L

PG_URL = os.environ.get("AZMONITOR_TEST_DATABASE_URL") or os.environ.get("AZMONITOR_DATABASE_URL")


def _conn():
    if not PG_URL:
        pytest.skip("no Postgres configured: set AZMONITOR_TEST_DATABASE_URL to run these")
    from azmonitor.cloud import readmodel as RM

    try:
        conn = RM.connect(PG_URL)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Postgres is not reachable: {exc}")
    RM.ensure_schema(conn)
    return conn


@pytest.fixture()
def clean():
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM job_locks WHERE name LIKE 'test-%'")
    conn.commit()
    yield conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM job_locks WHERE name LIKE 'test-%'")
    conn.commit()
    conn.close()


def _expire(conn, name: str) -> None:
    """Push a lease into the past, which is what a worker that stopped renewing looks like."""
    with conn.cursor() as cur:
        cur.execute("UPDATE job_locks SET expires_at = now() - interval '1 minute' WHERE name = %s",
                    (name,))
    conn.commit()


def test_the_fence_advances_every_time_the_lease_changes_hands(clean):
    a = L.DatabaseLease(clean, "test-fence", holder="worker-a", minutes=30)
    first = a.try_acquire()
    assert first["fence"] >= 1

    _expire(clean, "test-fence")
    b = L.DatabaseLease(clean, "test-fence", holder="worker-b", minutes=30)
    second = b.try_acquire()
    assert second["fence"] == first["fence"] + 1, "a fence token must never repeat or go backwards"


def test_a_replaced_worker_is_refused_before_it_can_write(clean):
    """The scenario the fence exists for, end to end."""
    slow = L.DatabaseLease(clean, "test-fence", holder="slow-worker", minutes=30)
    slow.try_acquire()
    slow.check("its first write")            # still fine

    # The slow worker stalls past its expiry and the next scheduled run takes over.
    _expire(clean, "test-fence")
    fresh = L.DatabaseLease(clean, "test-fence", holder="fresh-worker", minutes=30)
    fresh.try_acquire()

    # The slow worker wakes up and tries to save. It must not.
    with pytest.raises(L.LeaseLost, match="belongs to fresh-worker"):
        slow.check("saving the dataset")

    # The worker that actually owns it carries on.
    fresh.check("saving the dataset")


def test_a_worker_whose_lease_merely_expired_is_also_refused(clean):
    """Nobody has taken over yet, but an expired lease is still not ownership."""
    worker = L.DatabaseLease(clean, "test-fence", holder="worker", minutes=30)
    worker.try_acquire()
    _expire(clean, "test-fence")

    with pytest.raises(L.LeaseLost, match="expired"):
        worker.check("saving the dataset")


def test_a_deleted_lease_row_is_refused_rather_than_assumed(clean):
    worker = L.DatabaseLease(clean, "test-fence", holder="worker", minutes=30)
    worker.try_acquire()
    with clean.cursor() as cur:
        cur.execute("DELETE FROM job_locks WHERE name = 'test-fence'")
    clean.commit()

    with pytest.raises(L.LeaseLost, match="lease row has gone"):
        worker.check("saving the dataset")


def test_renewal_keeps_a_slow_but_healthy_worker_in_possession(clean):
    worker = L.DatabaseLease(clean, "test-fence", holder="worker", minutes=30)
    before = worker.try_acquire()
    worker.renew()
    after = worker.current()
    assert after["expires_at"] >= before["expires_at"]
    assert after["fence"] == before["fence"], "renewing is not reacquiring"
    worker.check("saving the dataset")


def test_renewal_fails_once_the_lease_has_been_taken(clean):
    """A worker that tries to renew too late learns it has been replaced, and says so."""
    slow = L.DatabaseLease(clean, "test-fence", holder="slow", minutes=30)
    slow.try_acquire()
    _expire(clean, "test-fence")
    L.DatabaseLease(clean, "test-fence", holder="fresh", minutes=30).try_acquire()

    with pytest.raises(L.LeaseLost):
        slow.renew()


def test_renewal_fails_on_an_expired_lease_nobody_else_has_taken(clean):
    """Expiry is the boundary, not "somebody else got there first".

    Renewing a lapsed lease would let a stalled worker resurrect ownership it no longer had, after
    a competing worker had already been told the lease was free.
    """
    worker = L.DatabaseLease(clean, "test-fence", holder="worker", minutes=30)
    worker.try_acquire()
    _expire(clean, "test-fence")

    with pytest.raises(L.LeaseLost):
        worker.renew()


def test_two_workers_cannot_both_take_a_lapsed_lease(clean):
    """Two connections race for the same expired lease; Postgres picks one."""
    from azmonitor.cloud import readmodel as RM

    first = L.DatabaseLease(clean, "test-fence", holder="incumbent", minutes=30)
    first.try_acquire()
    _expire(clean, "test-fence")

    other = RM.connect(PG_URL)
    try:
        a = L.DatabaseLease(clean, "test-fence", holder="challenger-a", minutes=30)
        b = L.DatabaseLease(other, "test-fence", holder="challenger-b", minutes=30)
        a.try_acquire()
        with pytest.raises(L.LeaseNotAcquired):
            b.try_acquire()
        # And the loser cannot write either.
        with pytest.raises(L.LeaseLost):
            b.check("saving the dataset")
    finally:
        other.close()


def test_a_crashed_worker_releases_nothing_and_the_next_run_takes_over(clean):
    """No cleanup runs when a machine disappears; the expiry is the whole recovery mechanism."""
    crashed = L.DatabaseLease(clean, "test-fence", holder="crashed", minutes=30)
    crashed.try_acquire()
    _expire(clean, "test-fence")                       # the machine went away

    successor = L.DatabaseLease(clean, "test-fence", holder="successor", minutes=30)
    info = successor.try_acquire()
    assert info["holder"] == "successor"
    successor.check("saving the dataset")


def test_a_fence_from_an_earlier_run_is_refused_even_by_the_same_holder(clean):
    """A retried job reuses its holder id. Without the fence it would look like the same worker.

    GitHub gives a re-run the same run id, so `github-actions/<run>.<attempt>` can repeat if a
    workflow is re-run. The fence is what distinguishes attempt two from a stalled attempt one.
    """
    first = L.DatabaseLease(clean, "test-fence", holder="github-actions/42.1", minutes=30)
    first.try_acquire()
    _expire(clean, "test-fence")
    second = L.DatabaseLease(clean, "test-fence", holder="github-actions/42.1", minutes=30)
    second.try_acquire()

    assert second.fence != first.fence
    with pytest.raises(L.LeaseLost, match="reacquired"):
        first.check("saving the dataset")
    second.check("saving the dataset")


def test_a_lease_with_no_fence_recorded_still_checks_holder_and_expiry(clean):
    """A worker configured from an older run has no fence to present; ownership still applies."""
    L.DatabaseLease(clean, "test-fence", holder="owner", minutes=30).try_acquire()

    blind = L.DatabaseLease(clean, "test-fence", holder="owner", minutes=30, fence=None)
    blind.check("saving the dataset")                  # same holder, unexpired: allowed

    stranger = L.DatabaseLease(clean, "test-fence", holder="stranger", minutes=30, fence=None)
    with pytest.raises(L.LeaseLost):
        stranger.check("saving the dataset")
