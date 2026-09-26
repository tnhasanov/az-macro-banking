"""The worker's side of a job: take it, keep it, report on it, finish it.

Every write a worker makes after claiming a job presents the fence it was given. A worker whose
lease lapsed — it hung, the runner was preempted, the network dropped long enough — finds the fence
has moved and gets `LeaseLost` instead of a silent success. That is the property that stops an old
attempt from marking a job finished, or publishing an edition, after a newer attempt took over.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from . import new_id

TERMINAL = ("succeeded", "reused", "unchanged", "waiting_for_data", "blocked", "failed", "cancelled")

# The stages a person sees, in order. A job reports the ones it passes through; a source check with
# nothing new goes queued -> collecting -> complete and never pretends to have rendered anything.
STAGES = ("queued", "starting", "waiting_for_dataset", "restoring", "collecting", "calculating",
          "writing_narrative", "rendering", "validating", "uploading", "publishing", "complete")


class LeaseLost(RuntimeError):
    """This worker no longer owns the job. Stop, and write nothing more."""


@dataclass
class Claim:
    job_id: str
    fence: int
    attempt: int
    max_attempts: int
    kind: str
    report_type: str | None
    params: dict[str, Any]
    trigger: str
    environment: str
    requested_by: str
    requester_email: str | None
    notify_requester: bool
    force_reason: str | None
    parent_job_id: str | None
    worker_id: str
    lost: threading.Event = field(default_factory=threading.Event)

    @classmethod
    def from_row(cls, row: dict[str, Any], worker_id: str) -> "Claim":
        return cls(job_id=row["job_id"], fence=int(row["fence"]), attempt=int(row["attempt"]),
                   max_attempts=int(row["max_attempts"]), kind=row["kind"],
                   report_type=row["report_type"], params=row["params"] or {}, trigger=row["trigger"],
                   environment=row["environment"], requested_by=row["requested_by"],
                   requester_email=row.get("requester_email"),
                   notify_requester=bool(row["notify_requester"]), force_reason=row["force_reason"],
                   parent_job_id=row["parent_job_id"], worker_id=worker_id)

    def check(self, what: str = "this write") -> None:
        if self.lost.is_set():
            raise LeaseLost(f"lease on {self.job_id} was lost before {what}")


def _rows(cur) -> list[dict[str, Any]]:
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _row(cur) -> dict[str, Any] | None:
    rows = _rows(cur)
    return rows[0] if rows else None


# ------------------------------------------------------------------ creating

def create_job(conn, *, kind: str, report_type: str | None, params: dict[str, Any], trigger: str,
               environment: str, requested_by: str, requester_email: str | None = None,
               notify_requester: bool = False, parent_job_id: str | None = None,
               schedule_slot: str | None = None, force_reason: str | None = None,
               nonce: str | None = None, dispatch: bool = True, status: str = "queued",
               worker_id: str | None = None) -> tuple[str, bool]:
    """Create a job, or return the equivalent one already active. Returns (job_id, created).

    `status='running'` with a `worker_id` creates a job already claimed by the caller — how a source
    check takes on the child reports it runs itself, on the dataset it already holds. Such a child
    carries a lease like any other, so if the parent dies the child is reaped and redispatched on its
    own rather than lost with it.
    """
    job_id = new_id("job")
    with conn.transaction():
        with conn.cursor() as cur:
            running = status == "running"
            cur.execute(
                """INSERT INTO report_jobs(job_id, kind, report_type, params, equivalence_key, trigger,
                       environment, parent_job_id, schedule_slot, requested_by, requester_email,
                       notify_requester, force_reason, status, stage, attempt, fence, worker_id,
                       lease_expires_at, heartbeat_at, started_at)
                   VALUES (%s,%s,%s,%s, azm_equivalence_key(%s,%s,%s,%s), %s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s, %s, %s, %s,
                           CASE WHEN %s THEN now() + interval '5 minutes' END,
                           CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END)
                   ON CONFLICT DO NOTHING
                   RETURNING job_id""",
                (job_id, kind, report_type, Jsonb(params), kind, report_type, Jsonb(params), nonce,
                 trigger, environment, parent_job_id, schedule_slot, requested_by, requester_email,
                 notify_requester, force_reason, status, "starting" if running else "queued",
                 1 if running else 0, 1 if running else 0, worker_id,
                 running, running, running))
            inserted = cur.fetchone()
            if not inserted:
                cur.execute(
                    """SELECT job_id FROM report_jobs
                        WHERE environment = %s
                          AND ((equivalence_key = azm_equivalence_key(%s,%s,%s,%s)
                                AND status IN ('queued','dispatched','running'))
                               OR (%s::text IS NOT NULL AND schedule_slot = %s))
                        ORDER BY requested_at DESC LIMIT 1""",
                    (environment, kind, report_type, Jsonb(params), nonce, schedule_slot, schedule_slot))
                existing = cur.fetchone()
                if existing:
                    return existing[0], False
                raise RuntimeError("job insert conflicted but no equivalent job was found")
            cur.execute(
                "INSERT INTO job_events(job_id, attempt, stage, status, message, detail) VALUES (%s,%s,%s,%s,%s,%s)",
                (job_id, 1 if running else 0, "starting" if running else "queued", status,
                 f"requested ({trigger})", Jsonb({"requested_by": requested_by, "params": params})))
            if dispatch and not running:
                cur.execute(
                    """INSERT INTO job_dispatches(dispatch_id, job_id, attempt, status)
                       VALUES (%s, %s, 1, 'pending') ON CONFLICT DO NOTHING""",
                    (new_id("dsp"), job_id))
    return job_id, True


# ------------------------------------------------------------------ owning

def claim(conn, job_id: str, *, worker_id: str, run_id: int | None = None,
          run_attempt: int | None = None, run_url: str | None = None,
          lease_seconds: int = 300) -> Claim | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM azm_claim_job(%s,%s,%s,%s,%s,%s)",
                    (job_id, worker_id, run_id, run_attempt, run_url, lease_seconds))
        row = _row(cur)
    conn.commit() if not conn.autocommit else None
    if not row or row.get("job_id") is None:
        return None
    return Claim.from_row(row, worker_id)


def adopt(conn, job_id: str, *, worker_id: str) -> Claim:
    """Take a child created already running by this worker (see create_job)."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM report_jobs WHERE job_id = %s AND worker_id = %s AND status = 'running'",
                    (job_id, worker_id))
        row = _row(cur)
    if not row:
        raise LeaseLost(f"{job_id} is not held by {worker_id}")
    return Claim.from_row(row, worker_id)


def heartbeat(conn, job_id: str, fence: int, lease_seconds: int = 300) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT azm_heartbeat(%s,%s,%s)", (job_id, fence, lease_seconds))
        ok = bool(cur.fetchone()[0])
    if not conn.autocommit:
        conn.commit()
    return ok


class Heartbeat:
    """Keeps every job this worker holds alive, on its own connection, until stopped.

    A separate connection because a psycopg connection must not be used from two threads at once,
    and because the main thread may sit inside a long transaction or a long subprocess — rendering
    a deck takes minutes — during which the lease must still be renewed.
    """

    def __init__(self, connect, *, interval: float = 30.0, lease_seconds: int = 300):
        self._connect = connect
        self.interval = interval
        self.lease_seconds = lease_seconds
        self._claims: dict[str, Claim] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.beats = 0
        self.errors = 0

    def add(self, claim: Claim) -> None:
        with self._lock:
            self._claims[claim.job_id] = claim

    def remove(self, claim: Claim) -> None:
        with self._lock:
            self._claims.pop(claim.job_id, None)

    def _run(self) -> None:
        conn = None
        while not self._stop.wait(self.interval):
            try:
                if conn is None or conn.closed:
                    conn = self._connect()
                    conn.autocommit = True
                with self._lock:
                    held = list(self._claims.values())
                for c in held:
                    if not heartbeat(conn, c.job_id, c.fence, self.lease_seconds):
                        c.lost.set()
                        with self._lock:
                            self._claims.pop(c.job_id, None)
                self.beats += 1
            except Exception:  # a failed beat is retried at the next interval; the lease has slack
                self.errors += 1
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
        if conn is not None:
            conn.close()

    def start(self) -> "Heartbeat":
        self._thread = threading.Thread(target=self._run, name="job-heartbeat", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 5)


# ------------------------------------------------------------------ reporting

def _guarded(cur, claim: Claim, what: str) -> None:
    """Lock the job row and confirm this worker still owns it, or raise LeaseLost."""
    claim.check(what)
    cur.execute("SELECT fence, status FROM report_jobs WHERE job_id = %s FOR UPDATE", (claim.job_id,))
    row = cur.fetchone()
    if not row or int(row[0]) != claim.fence or row[1] != "running":
        claim.lost.set()
        raise LeaseLost(f"{claim.job_id} moved on before {what} "
                        f"(fence {row[0] if row else None}, status {row[1] if row else None})")


def set_stage(conn, claim: Claim, stage: str, detail: str | None = None) -> None:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    with conn.transaction():
        with conn.cursor() as cur:
            _guarded(cur, claim, f"stage {stage}")
            cur.execute(
                """UPDATE report_jobs SET stage = %s, stage_detail = %s,
                          stage_started_at = CASE WHEN stage IS DISTINCT FROM %s THEN now()
                                                  ELSE stage_started_at END,
                          heartbeat_at = now()
                    WHERE job_id = %s""",
                (stage, detail, stage, claim.job_id))
            cur.execute(
                "INSERT INTO job_events(job_id, attempt, stage, status, message) VALUES (%s,%s,%s,'running',%s)",
                (claim.job_id, claim.attempt, stage, detail))


def set_detail(conn, claim: Claim, detail: str) -> None:
    """Measured progress within a stage — "dataset 12 of 31" — without a new event per step."""
    with conn.cursor() as cur:
        cur.execute("UPDATE report_jobs SET stage_detail = %s WHERE job_id = %s AND fence = %s AND status = 'running'",
                    (detail, claim.job_id, claim.fence))
    if not conn.autocommit:
        conn.commit()


def note(conn, claim: Claim, message: str, detail: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_events(job_id, attempt, stage, status, message, detail) "
            "SELECT %s, %s, stage, status, %s, %s FROM report_jobs WHERE job_id = %s",
            (claim.job_id, claim.attempt, message, Jsonb(detail or {}), claim.job_id))
    if not conn.autocommit:
        conn.commit()


def finish(conn, claim: Claim, status: str, *, result: dict[str, Any] | None = None,
           edition_id: str | None = None, error_code: str | None = None,
           error_message: str | None = None, error_detail: str | None = None) -> None:
    """Record a terminal outcome other than a publication (see publish.commit for that one)."""
    if status not in TERMINAL:
        raise ValueError(f"{status!r} is not a terminal status")
    with conn.transaction():
        with conn.cursor() as cur:
            _guarded(cur, claim, f"finishing as {status}")
            cur.execute(
                """UPDATE report_jobs SET status = %s, stage = CASE WHEN %s IN ('failed','blocked','cancelled')
                                                                 THEN stage ELSE 'complete' END,
                          finished_at = now(), lease_expires_at = NULL, result = result || %s,
                          edition_id = coalesce(%s, edition_id), error_code = %s, error_message = %s,
                          error_detail = %s
                    WHERE job_id = %s""",
                (status, status, Jsonb(result or {}), edition_id, error_code, error_message,
                 (error_detail or "")[:4000] or None, claim.job_id))
            cur.execute(
                "INSERT INTO job_events(job_id, attempt, stage, status, message, detail) VALUES (%s,%s,%s,%s,%s,%s)",
                (claim.job_id, claim.attempt, "complete" if status not in ("failed", "blocked", "cancelled") else None,
                 status, error_message or status, Jsonb({"edition_id": edition_id} if edition_id else {})))


def retry_later(conn, claim: Claim, *, error_code: str, error_message: str,
                error_detail: str | None = None) -> str:
    """A failure worth another attempt: requeue with backoff, or fail once attempts are spent."""
    with conn.transaction():
        with conn.cursor() as cur:
            _guarded(cur, claim, "scheduling a retry")
            if claim.attempt < claim.max_attempts:
                delay = 60 * (4 ** claim.attempt)
                cur.execute(
                    """UPDATE report_jobs SET status = 'queued', stage = 'queued',
                              stage_detail = %s, fence = fence + 1, worker_id = NULL,
                              lease_expires_at = NULL,
                              next_attempt_at = now() + make_interval(secs => %s),
                              error_code = %s, error_message = %s, error_detail = %s
                        WHERE job_id = %s""",
                    (f"retrying after: {error_message}", delay, error_code, error_message,
                     (error_detail or "")[:4000] or None, claim.job_id))
                cur.execute(
                    """INSERT INTO job_dispatches(dispatch_id, job_id, attempt, status, next_try_at)
                       VALUES (%s,%s,%s,'pending', now() + make_interval(secs => %s)) ON CONFLICT DO NOTHING""",
                    (new_id("dsp"), claim.job_id, claim.attempt + 1, delay))
                outcome = "requeued"
            else:
                cur.execute(
                    """UPDATE report_jobs SET status = 'failed', finished_at = now(), lease_expires_at = NULL,
                              error_code = %s, error_message = %s, error_detail = %s
                        WHERE job_id = %s""",
                    (error_code, error_message, (error_detail or "")[:4000] or None, claim.job_id))
                outcome = "failed"
            cur.execute(
                "INSERT INTO job_events(job_id, attempt, status, message) VALUES (%s,%s,%s,%s)",
                (claim.job_id, claim.attempt, "queued" if outcome == "requeued" else "failed", error_message))
    claim.lost.set()      # either way this worker is done with it
    return outcome


def record_fingerprint(conn, *, environment: str, report_type: str, scope_key: str,
                       fingerprint: str, edition_id: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO current_fingerprints(environment, report_type, scope_key, fingerprint, edition_id)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (environment, report_type, scope_key) DO UPDATE
                 SET fingerprint = excluded.fingerprint, edition_id = excluded.edition_id, computed_at = now()""",
            (environment, report_type, scope_key, fingerprint, edition_id))
    if not conn.autocommit:
        conn.commit()


def get_job(conn, job_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM report_jobs WHERE job_id = %s", (job_id,))
        return _row(cur)


def events(conn, job_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job_events WHERE job_id = %s ORDER BY id", (job_id,))
        return _rows(cur)


def reap(conn, environment: str, *, unclaimed_grace_seconds: int = 1800) -> list[dict[str, Any]]:
    """The same recovery the scheduler tick runs, for tests and for local operation."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM azm_reap(%s, %s)", (environment, unclaimed_grace_seconds))
            return _rows(cur)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
