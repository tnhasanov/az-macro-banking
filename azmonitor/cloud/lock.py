"""A lease in the database, because a file lock on an ephemeral disk coordinates nothing.

The engine's `JobLock` is an `O_EXCL` file with a PID in it. On one persistent host that is exactly
right: the file outlives the process, and a stale lock can be checked against whether its holder is
still alive. Move the same run to a pool of disposable workers and both properties vanish — each
worker has its own disk, so each sees an unlocked directory, and a PID from another machine means
nothing.

So coordination moves to the one thing every worker shares. A lease is a row holding a name, a
holder and an expiry:

* **Acquiring** is a conditional insert. Postgres decides who wins, once, under its own
  serialisation — there is no window in which two workers both believe they hold it.
* **Expiry replaces liveness.** We cannot ask whether a holder on another machine is alive, so a
  lease is held for a bounded time and renewed while the work continues. A worker that dies stops
  renewing and the lease lapses on its own.
* **Renewal is not automatic.** A long job renews as it goes, and a job that stops renewing loses
  the lease — which is the point. A lease that renewed itself in a background thread would outlive
  a wedged job for ever.
* **Fencing decides what happens when renewal was not enough.** Renewal narrows the window in which
  a healthy-but-slow job loses its lease; it cannot close it, because a job can always be paused
  for longer than its remaining lease. So every acquisition stamps a monotonically increasing fence
  token, and the worker re-checks holder *and* token immediately before each persistent write. A
  worker that lost the lease finds a token it does not recognise and stops, instead of racing the
  worker that replaced it. Without this, the 60-minute job timeout and the 55-minute lease left a
  five-minute window in which two workers could both write the dataset.

The file lock stays where it is. On a single host it is still the right mechanism, and a deployment
that uses one should not pay for a database round trip to learn what a local file already knows.
"""
from __future__ import annotations

import datetime as dt
import os
import socket
import uuid
from typing import Any

from ..util.log import get_logger

log = get_logger("cloud.lock")

DEFAULT_LEASE_MINUTES = 30


class LeaseLost(RuntimeError):
    """The lease was not renewed in time and now belongs to someone else, or to nobody.

    Raised rather than returned because continuing to write to a shared dataset without the lease is
    the corruption this exists to prevent.
    """


class LeaseNotAcquired(RuntimeError):
    """Someone else holds it. Normal, and not a failure: the other run is doing the work."""


def holder_id() -> str:
    """Who is asking, in terms that survive the machine going away.

    The GitHub run id is used when there is one, because it is the thing an operator looking at a
    stuck lease can actually open.
    """
    run = os.environ.get("GITHUB_RUN_ID")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if run:
        return f"github-actions/{run}.{attempt}"
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


class DatabaseLease:
    """A named lease with an expiry, acquired and renewed against Postgres."""

    def __init__(self, conn, name: str = "azmonitor-run", *,
                 minutes: int = DEFAULT_LEASE_MINUTES, holder: str | None = None,
                 detail: dict[str, Any] | None = None, fence: int | None = None):
        self.conn = conn
        self.name = name
        self.minutes = int(minutes)
        self.holder = holder or holder_id()
        self.detail = detail or {}
        self.acquired = False
        self.fence = int(fence) if fence is not None else None

    # ------------------------------------------------------------------ acquiring
    def try_acquire(self) -> dict[str, Any]:
        """Take the lease if it is free or expired. One statement, so the database arbitrates.

        `ON CONFLICT ... WHERE expires_at < now()` is what makes this safe: the row is only replaced
        when the existing lease has genuinely lapsed, and two workers racing to replace the same
        expired lease still produce exactly one winner.

        Each acquisition bumps `fence`, which never goes backwards. The worker carries that number
        and presents it before every persistent write, so a worker whose lease lapsed can be told
        apart from the one that took over — by the database, not by either of them guessing.
        """
        import json

        with self.conn.cursor() as cur:
            row = cur.execute(
                "INSERT INTO job_locks (name, holder, acquired_at, expires_at, detail, fence) "
                "VALUES (%s, %s, now(), now() + make_interval(mins => %s), %s, 1) "
                "ON CONFLICT (name) DO UPDATE SET "
                "  holder = EXCLUDED.holder, acquired_at = EXCLUDED.acquired_at, "
                "  expires_at = EXCLUDED.expires_at, detail = EXCLUDED.detail, "
                "  fence = job_locks.fence + 1 "
                "WHERE job_locks.expires_at < now() "
                "RETURNING holder, acquired_at, expires_at, fence",
                (self.name, self.holder, self.minutes, json.dumps(self.detail))).fetchone()
        self.conn.commit()
        if row is None:
            current = self.current()
            raise LeaseNotAcquired(
                f"{self.name} is held by {current.get('holder')} until {current.get('expires_at')}; "
                f"this run is doing nothing rather than working on the same dataset")
        self.acquired = True
        self.fence = int(row[3])
        log.info("lease %s acquired by %s until %s (fence %s)", self.name, row[0], row[2], self.fence)
        return {"name": self.name, "holder": row[0], "acquired_at": row[1], "expires_at": row[2],
                "fence": self.fence}

    def current(self) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            row = cur.execute(
                "SELECT holder, acquired_at, expires_at, detail, expires_at < now() AS expired, fence "
                "FROM job_locks WHERE name = %s", (self.name,)).fetchone()
        if not row:
            return {}
        return {"holder": row[0], "acquired_at": row[1], "expires_at": row[2], "detail": row[3],
                "expired": row[4], "fence": row[5]}

    # ------------------------------------------------------------------- renewing
    def renew(self) -> None:
        """Push the expiry out. Raises if the lease is no longer ours.

        Called from the work itself rather than a timer: a background renewer would keep a wedged
        job's lease alive indefinitely, which is precisely the situation the expiry exists to end.
        """
        with self.conn.cursor() as cur:
            row = cur.execute(
                "UPDATE job_locks SET expires_at = now() + make_interval(mins => %s) "
                "WHERE name = %s AND holder = %s AND expires_at > now() "
                "  AND (%s::bigint IS NULL OR fence = %s) "
                "RETURNING expires_at, fence",
                (self.minutes, self.name, self.holder, self.fence, self.fence)).fetchone()
        self.conn.commit()
        if row is None:
            self.acquired = False
            raise LeaseLost(
                f"the {self.name} lease is no longer held by {self.holder}; another run has taken it "
                f"and this one must stop writing")
        log.debug("lease %s renewed until %s", self.name, row[0])

    # ------------------------------------------------------------------- fencing
    def check(self, what: str = "this write") -> None:
        """Refuse to go on unless this worker still owns the lease it started with.

        Called immediately before anything persistent. The cost is one indexed row read; the thing
        it prevents is a worker that was paused past its expiry waking up and overwriting the work
        of the worker that replaced it.
        """
        current = self.current()
        if not current:
            raise LeaseLost(
                f"refusing {what}: the {self.name} lease row has gone, so this worker cannot show "
                f"it still owns the dataset")
        if current["holder"] != self.holder:
            raise LeaseLost(
                f"refusing {what}: the {self.name} lease now belongs to {current['holder']}, not to "
                f"{self.holder}; this worker was replaced and must not write")
        if self.fence is not None and current.get("fence") != self.fence:
            raise LeaseLost(
                f"refusing {what}: the {self.name} lease has been reacquired since this worker took "
                f"it (fence {current.get('fence')}, expected {self.fence})")
        if current.get("expired"):
            raise LeaseLost(
                f"refusing {what}: the {self.name} lease expired at {current['expires_at']}; renew "
                f"it or stop")

    def release(self) -> None:
        """Give it back early. A lease that is never released simply expires."""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM job_locks WHERE name = %s AND holder = %s",
                        (self.name, self.holder))
        self.conn.commit()
        self.acquired = False
        log.info("lease %s released by %s", self.name, self.holder)

    # ----------------------------------------------------------------- as a block
    def __enter__(self) -> "DatabaseLease":
        self.try_acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            try:
                self.release()
            except Exception:  # pragma: no cover - releasing must never mask the real error
                log.exception("could not release the %s lease; it will expire on its own", self.name)


def expired_leases(conn) -> list[dict[str, Any]]:
    """Leases whose holder has gone away, for the monitoring page.

    A lease that keeps expiring is a job that keeps dying, which is worth seeing even though the
    next run takes over cleanly.
    """
    with conn.cursor() as cur:
        rows = cur.execute(
            "SELECT name, holder, acquired_at, expires_at FROM job_locks WHERE expires_at < now()"
        ).fetchall()
    return [{"name": r[0], "holder": r[1], "acquired_at": r[2], "expires_at": r[3]} for r in rows]
