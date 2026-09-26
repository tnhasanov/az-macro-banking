"""The job worker: take one job, do it with the real engine, and leave a truthful record.

    python -m azmonitor.jobs run --job-id job_...

This is the only thing a dispatched runner executes. It is given a job id and nothing else; the
request itself — which report, which period, whether to check the sources first — is read from the
job row, validated again, and never taken from the runner's inputs. So a dispatch can only ever
start work somebody already asked for, and the worst a forged dispatch can do is run a job early.

One pipeline, whatever started it
---------------------------------
A request from the browser, a scheduled source check and the Monday digest all arrive here as jobs
and go through the same steps:

    claim  ->  hold the dataset  ->  restore  ->  [check sources]  ->  produce  ->  validate
          ->  upload and verify  ->  publish (one transaction)  ->  save the dataset

A source check is a job whose "produce" step is a plan: the reports its classified changes affect.
Each planned report becomes a child job, created already owned by this worker and run here on the
dataset this worker already holds. The child has its own lease and its own record, so a person
sees "Monthly Monitor: rendering" rather than "source check: busy", and a child whose parent dies
is recovered on its own.

What "done" means
-----------------
Generation, publication and notification are separate facts. A job succeeds when its edition is
published: validated, uploaded, verified, and recorded in the same transaction that queues its
email. It ends `reused` when the answer is an edition that already exists with identical inputs,
`unchanged` when a scheduled check found nothing new, `waiting_for_data` when the data a period
needs has not been published, `blocked` when a validation or quality rule stopped publication, and
`failed` when something went wrong that retrying will not fix. Nothing is left `running`: a worker
that dies stops renewing its lease and the scheduler's reaper takes the job back.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import socket
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import changes as CH
from .. import config
from ..appstate import JOB_ID_PATTERN, jobs as J, new_id, publication as P
from ..appstate.jobs import Claim, JobCancelled, LeaseLost
from ..util import progress
from ..util.log import get_logger
from . import outputs as O
from . import params as PR

log = get_logger("jobs.worker")

DATASET_LEASE = "azmonitor-run"
BAKU = dt.timezone(dt.timedelta(hours=4))


class Outcome(Exception):
    """A terminal answer that is not a publication, with a sentence a person can act on."""

    def __init__(self, status: str, code: str, message: str, *, result: dict[str, Any] | None = None,
                 edition_id: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.result = result or {}
        self.edition_id = edition_id


class Transient(Exception):
    """Worth another attempt later: storage, network or database trouble, or the dataset is busy."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _transient(exc: BaseException) -> Transient | None:
    from ..cloud.lock import LeaseNotAcquired
    from ..cloud.objectstore import StorageError

    if isinstance(exc, Transient):
        return exc
    if isinstance(exc, StorageError):
        return Transient("storage_unavailable", f"Private storage could not be reached: {exc}")
    if isinstance(exc, LeaseNotAcquired):
        return Transient("dataset_busy", "Another job was using the dataset for longer than this one could wait.")
    try:
        import psycopg

        if isinstance(exc, psycopg.OperationalError):
            return Transient("database_unavailable", "The application database could not be reached.")
    except ImportError:  # pragma: no cover
        pass
    try:
        import requests

        if isinstance(exc, requests.RequestException):
            return Transient("network", f"A network request failed: {type(exc).__name__}")
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return Transient("network", f"A network operation failed: {type(exc).__name__}")
    return None


@dataclass
class Item:
    """One report to produce: the job's own, or one a source check planned."""
    report_type: str
    params: dict[str, Any]
    cause: str
    announce: bool = False
    change_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    planned: bool = False
    scope: str = ""


@dataclass
class Settings:
    lease_seconds: int = 300
    heartbeat_interval: float = 30.0
    heartbeat_max_seconds: float = 100 * 60
    dataset_lease_minutes: int = 30
    dataset_wait_seconds: float = 20 * 60
    dataset_poll_seconds: float = 15.0
    skip_restore: bool = False
    save_dataset: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.skip_restore = os.environ.get("AZMONITOR_SKIP_RESTORE") == "1"
        s.save_dataset = os.environ.get("AZMONITOR_SKIP_SAVE") != "1"
        if os.environ.get("AZMONITOR_DATASET_WAIT_SECONDS"):
            s.dataset_wait_seconds = float(os.environ["AZMONITOR_DATASET_WAIT_SECONDS"])
        return s


def worker_id() -> str:
    run = os.environ.get("GITHUB_RUN_ID")
    if run:
        return f"github-actions/{run}.{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}/{uuid.uuid4().hex[:6]}"
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(self, *, connect: Callable[[], Any], engine=None, store=None, settings: Settings | None = None,
                 worker: str | None = None, run_id: int | None = None, run_attempt: int | None = None,
                 run_url: str | None = None, today: dt.date | None = None):
        from .engine import Engine

        self.connect = connect
        self.engine = engine or Engine()
        self._store = store
        self.settings = settings or Settings.from_env()
        self.worker_id = worker or worker_id()
        self.run_id, self.run_attempt, self.run_url = run_id, run_attempt, run_url
        self.today = today
        self.conn = None
        self.heartbeat: J.Heartbeat | None = None
        self.lease = None
        self._lease_renewed = 0.0
        self._detail_at = 0.0
        self._dataset_dirty = False
        self._collected = False
        self._produced: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ plumbing
    @property
    def store(self):
        if self._store is None:
            from ..cloud import objectstore as OS

            self._store = OS.store_from_env()
        return self._store

    def _today(self) -> dt.date:
        return self.today or dt.datetime.now(BAKU).date()

    def _progress(self, claim: Claim):
        def on_stage(name: str, detail: str | None) -> None:
            self._renew_dataset_lease()
            if name in J.STAGES:
                J.set_stage(self.conn, claim, name, detail)

        def on_detail(text: str) -> None:
            self._renew_dataset_lease()
            now = time.monotonic()
            if now - self._detail_at >= 3.0:
                self._detail_at = now
                claim.check("reporting progress")
                J.set_detail(self.conn, claim, text)

        return progress.reporting(on_stage, on_detail)

    def _renew_dataset_lease(self, force: bool = False) -> None:
        if self.lease is None:
            return
        if force or time.monotonic() - self._lease_renewed > 120:
            self.lease.renew()
            self._lease_renewed = time.monotonic()

    # ------------------------------------------------------------------ entry point
    def run(self, job_id: str) -> dict[str, Any]:
        if not re.match(JOB_ID_PATTERN, job_id or ""):
            raise ValueError(f"{job_id!r} is not a job id")
        self.conn = self.connect()
        self.conn.autocommit = True
        try:
            claim = J.claim(self.conn, job_id, worker_id=self.worker_id, run_id=self.run_id,
                            run_attempt=self.run_attempt, run_url=self.run_url,
                            lease_seconds=self.settings.lease_seconds)
            if claim is None:
                row = J.get_job(self.conn, job_id)
                return {"job_id": job_id, "claimed": False, "status": row and row["status"],
                        "note": "the job is finished, cancelled, out of attempts or held by another worker"}
            self.heartbeat = J.Heartbeat(self.connect, interval=self.settings.heartbeat_interval,
                                         lease_seconds=self.settings.lease_seconds,
                                         max_seconds=self.settings.heartbeat_max_seconds).start()
            self.heartbeat.add(claim)
            try:
                return self._run_claimed(claim)
            finally:
                self.heartbeat.stop()
        finally:
            self.conn.close()

    def _run_claimed(self, claim: Claim) -> dict[str, Any]:
        outcome: dict[str, Any] = {"job_id": claim.job_id, "claimed": True, "attempt": claim.attempt}
        try:
            self._check_request(claim)
            J.set_stage(self.conn, claim, "starting", f"worker {self.worker_id}")
            self._hold_dataset(claim)
            try:
                self._restore(claim)
                self.engine.open()
                try:
                    self._sync()
                    outcome.update(self._work(claim))
                finally:
                    self.engine.close()
                try:
                    self._save_dataset(claim)
                    if self._collected and self.settings.save_dataset and not self.settings.skip_restore:
                        self._refresh_readmodel(claim)
                except LeaseLost:
                    raise
                except Exception as exc:
                    # Every publication is already recorded in Postgres, and the next worker writes
                    # it back into the dataset before it runs (see _sync). What is lost is this
                    # run's collected data, which the next check collects again.
                    log.exception("the dataset could not be saved after the work was recorded")
                    J.note(self.conn, claim, "the dataset could not be saved; the next run restores the "
                           "previous one and re-applies every published edition",
                           {"error": f"{type(exc).__name__}: {exc}"})
                    outcome["dataset_save_failed"] = True
            finally:
                self._release_dataset()
            return outcome
        except LeaseLost as exc:
            log.warning("%s", exc)
            return {**outcome, "lease_lost": str(exc)}
        except JobCancelled:
            self._finish_quietly(claim, "cancelled", error_code="cancelled",
                                 error_message="Cancelled at the request of a signed-in user.")
            return {**outcome, "status": "cancelled"}
        except Outcome as o:
            self._finish_quietly(claim, o.status, result=o.result, edition_id=o.edition_id,
                                 error_code=None if o.status in ("reused", "unchanged") else o.code,
                                 error_message=o.message)
            return {**outcome, "status": o.status, "code": o.code, "message": o.message}
        except Exception as exc:  # the last line: nothing leaves a job "running"
            detail = traceback.format_exc()
            t = _transient(exc)
            if t is not None and not claim.lost.is_set():
                try:
                    what = J.retry_later(self.conn, claim, error_code=t.code, error_message=t.message,
                                         error_detail=detail)
                except LeaseLost:
                    what = "lease_lost"
                return {**outcome, "status": what, "code": t.code, "message": t.message}
            log.exception("job %s failed", claim.job_id)
            self._finish_quietly(claim, "failed", error_code="engine_error",
                                 error_message=f"The report engine stopped with {type(exc).__name__}. "
                                               "The run log has the details; retrying will run it again.",
                                 error_detail=detail)
            return {**outcome, "status": "failed", "code": "engine_error", "message": str(exc)}

    def _finish_quietly(self, claim: Claim, status: str, **kw) -> None:
        """Record a terminal state, unless the job already has one or belongs to someone else."""
        try:
            J.finish(self.conn, claim, status, **kw)
        except LeaseLost:
            pass

    # ------------------------------------------------------------------ the request
    def _check_request(self, claim: Claim) -> None:
        if claim.kind not in ("report", "source_check", "weekly_digest"):
            raise Outcome("failed", "unsupported_job", f"This worker does not run {claim.kind!r} jobs.")
        if claim.kind in ("source_check", "weekly_digest"):
            return
        try:
            canonical = PR.normalise(claim.report_type or "", claim.params)
        except PR.InvalidRequest as exc:
            raise Outcome("failed", exc.code, exc.message) from None
        if canonical != claim.params:
            raise Outcome("failed", "invalid_request",
                          "The stored request is not in its canonical form, so it was not run.")

    # ------------------------------------------------------------------ the dataset
    def _hold_dataset(self, claim: Claim) -> None:
        from ..cloud import readmodel as RM
        from ..cloud.lock import DatabaseLease, LeaseNotAcquired

        J.set_stage(self.conn, claim, "waiting_for_dataset", "one job at a time works on the dataset")
        lease_conn = self.connect()
        lease_conn.autocommit = True
        RM.ensure_schema(lease_conn)
        lease = DatabaseLease(lease_conn, DATASET_LEASE, minutes=self.settings.dataset_lease_minutes,
                              holder=f"{claim.job_id}.{claim.attempt}/{self.worker_id}",
                              detail={"job_id": claim.job_id, "attempt": claim.attempt})
        deadline = time.monotonic() + self.settings.dataset_wait_seconds
        while True:
            claim.check("waiting for the dataset")
            try:
                lease.try_acquire()
                break
            except LeaseNotAcquired as exc:
                if time.monotonic() >= deadline:
                    lease_conn.close()
                    raise Transient("dataset_busy", str(exc)) from None
                J.set_detail(self.conn, claim, f"waiting: {exc}".split(";")[0])
                time.sleep(self.settings.dataset_poll_seconds)
        self.lease = lease
        self._lease_renewed = time.monotonic()

    def _release_dataset(self) -> None:
        if self.lease is not None:
            try:
                self.lease.release()
            except Exception:  # pragma: no cover - it expires on its own
                log.exception("could not release the dataset lease")
            try:
                self.lease.conn.close()
            except Exception:  # pragma: no cover
                pass
            self.lease = None

    def _restore(self, claim: Claim) -> None:
        from ..cloud import objectstore as OS
        from ..storage.backup import verify_dataset

        paths = config.paths()
        paths.ensure()
        if self.settings.skip_restore:
            if claim.environment == "production":
                raise Outcome("failed", "misconfigured",
                              "This worker is set to skip restoring the dataset, which production never allows.")
            J.note(self.conn, claim, "restore skipped: working on the dataset already on this machine")
        else:
            J.set_stage(self.conn, claim, "restoring", "downloading the dataset from private storage")
            res = OS.restore_dataset(paths.data_dir, self.store)
            if not res.get("restored"):
                raise Outcome("failed", "dataset_unavailable",
                              "Private storage holds no dataset yet, so there is nothing to report from. "
                              "Seed it once (docs/report-jobs.md) and try again.")
        check = verify_dataset(paths.db_path, min_documents=1)
        if not check.get("ok"):
            raise Outcome("failed", "dataset_invalid",
                          "The restored dataset failed its integrity check: " + "; ".join(check.get("problems") or []))

    def _save_dataset(self, claim: Claim) -> None:
        if not self._dataset_dirty or not self.settings.save_dataset or self.settings.skip_restore:
            return
        from ..cloud import objectstore as OS

        self._renew_dataset_lease(force=True)
        saved = OS.save_dataset(config.paths().data_dir, self.store, fence=self.lease)
        self._dataset_dirty = False
        try:
            J.note(self.conn, claim, "dataset saved", {"saved_at": saved.get("saved_at"),
                                                       "uploaded_bytes": saved.get("uploaded_bytes")})
        except Exception:  # pragma: no cover - a note is not worth failing over
            pass

    def _refresh_readmodel(self, claim: Claim) -> None:
        """The dashboard's figures, rebuilt from the dataset this worker just saved, under its lease."""
        from ..cloud.publish import refresh_readmodel

        self._renew_dataset_lease(force=True)
        conn = self.connect()
        try:
            counts = refresh_readmodel(conn, self.store)
            J.note(self.conn, claim, "dashboard figures refreshed",
                   {k: v for k, v in counts.items() if isinstance(v, (int, float, str))})
        except Exception as exc:  # the figures lag one run; the reports are already published
            log.exception("the read model could not be refreshed")
            J.note(self.conn, claim, "the dashboard figures could not be refreshed; they update on the next run",
                   {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()

    def _sync(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT edition_id, report_type, edition, version, fingerprint, published_at, "
                        "manifest, reporting_periods FROM publication_records ORDER BY published_at")
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        added = self.engine.sync_published(rows)
        if added:
            self._dataset_dirty = True
            log.info("wrote %d published edition(s) back into the dataset", added)

    # ------------------------------------------------------------------ the work
    def _work(self, claim: Claim) -> dict[str, Any]:
        refreshed: dict[str, Any] | None = None
        quality: dict[str, Any] | None = None
        checks_sources = claim.kind == "source_check" or claim.params.get("refresh") == "check_sources"
        if checks_sources:
            refreshed, quality = self._check_sources(claim)
        self._publish_availability()

        pending = CH.pending(self.conn, environment=claim.environment)
        # A productive change that no report is mapped to is recorded, and closed: there is nothing
        # to produce from it. config/triggers.yaml lists every dataset deliberately left unmapped.
        CH.mark_handled(self.conn, [c.change_id for c in pending if not c.affects], job_id=claim.job_id,
                        handling="no_report")
        plan = CH.plan(pending)
        items: list[tuple[Item, Claim | None]] = []
        own: Item | None = None
        if claim.kind == "report":
            own = self._own_item(claim, plan, quality)
            items.append((own, claim))
        elif claim.kind == "weekly_digest":
            own = Item("weekly", {"period": "latest", "refresh": "latest_data"}, "weekly", scope="latest")
            items.append((own, claim))

        waiting: list[dict[str, Any]] = []
        if checks_sources:
            for p in plan:
                if own is not None and (p.report_type, p.scope) == (own.report_type, own.scope):
                    continue
                item = Item(p.report_type, self._planned_params(p), p.cause if p.announce else "coverage_refresh",
                            p.announce, list(p.change_ids), list(p.reasons), planned=True, scope=p.scope)
                ready = self.engine.readiness(item.report_type, item.params, self._today(), quality)
                if ready is not None and not ready.get("ok"):
                    waiting.append({"report_type": item.report_type, "scope": p.scope, "state": ready.get("state"),
                                    "reasons": [r for r in ready.get("reasons", []) if not r.get("met")][:4],
                                    "missing": ready.get("missing", [])})
                    continue
                items.append((item, None))

        results: list[dict[str, Any]] = []
        children = [i for i, c in items if c is None]
        for item, item_claim in items:
            if item_claim is None:
                if own is None:
                    J.set_stage(self.conn, claim, "calculating",
                                f"producing {len(results) + 1} of {len(children)} affected report(s): "
                                f"{PR.describe(item.report_type, item.params)}")
                results.append(self._run_child(claim, item))
            else:
                results.append(self._produce_own(item_claim, item))

        self._close_changes(plan, items, results, waiting)
        summary = {"items": results, "waiting": waiting,
                   "changes": refreshed.get("summary") if refreshed else None}
        if claim.kind == "source_check":
            published = [r for r in results if r.get("status") == "published"]
            status = "succeeded" if published else "unchanged"
            message = (f"published {len(published)} edition(s)" if published
                       else "no relevant change: nothing was published and nobody was emailed")
            J.finish(self.conn, claim, status, result={**summary, "message": message})
            return {"status": status, **summary}
        return {"status": results[0].get("status") if results else None, **summary}

    def _check_sources(self, claim: Claim) -> tuple[dict[str, Any], dict[str, Any]]:
        from ..cloud import readmodel as RM
        from ..storage.backup import backup_database

        J.set_stage(self.conn, claim, "collecting", "checking the official sources")
        try:
            backup_database(config.paths().db_path, config.paths().data_dir / "backups", keep=3)
        except Exception:  # a local backup is a convenience; the stored snapshots are the history
            log.exception("local backup before the refresh failed")
        batch_id = new_id("chk")
        with self._progress(claim):
            refresh = self.engine.refresh()
        failed = {k: v.get("errors") for k, v in (refresh.get("datasets") or {}).items() if v.get("errors")}
        if refresh.get("datasets") and len(failed) == len(refresh["datasets"]):
            raise Transient("sources_unreachable", "Every official source failed to respond; the check will be retried.")
        self._dataset_dirty = True
        self._collected = True
        quality = self.engine.validate()
        found = CH.classify_refresh(refresh, batch_id=batch_id, today=self._today())
        CH.record(self.conn, found, batch_id=batch_id, environment=claim.environment)
        summary = CH.summarise(found)
        J.note(self.conn, claim, "source check complete",
               {"batch_id": batch_id, "changes": summary, "failed_datasets": sorted(failed)})
        try:
            rm = self.connect()
            try:
                RM.set_meta(rm, "last_source_check", {
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "job_id": claim.job_id, "batch_id": batch_id, "changes": summary,
                    "datasets_checked": len(refresh.get("datasets") or {}), "failed_datasets": sorted(failed)})
            finally:
                rm.close()
        except Exception:
            log.exception("could not record the source check in the read model")
        # Save straight away: what was collected is kept even if producing a report fails later.
        self._save_dataset(claim)
        return {"batch_id": batch_id, "summary": summary, "failed": failed}, quality

    def _publish_availability(self) -> None:
        from ..cloud import readmodel as RM

        try:
            rm = self.connect()
            try:
                RM.set_meta(rm, "data_availability", self.engine.availability(self._today()))
            finally:
                rm.close()
        except Exception:
            log.exception("could not publish data availability")

    def _scope(self, rt: str, params: dict[str, Any]) -> str:
        """What an edition is of, with "the latest publication" resolved to the one it is today."""
        scope = PR.scope_of(rt, params)
        if rt in ("mpr_brief", "fsr_brief", "decision_update") and scope == "latest":
            pub = self.engine.resolve_publication(rt, "latest")
            if pub:
                return pub["publication_id"]
        if rt == "weekly" and scope == "latest":
            # "the latest week" is a different week every Monday; the edition is of a named week
            from ..scheduling.tasks import previous_calendar_week

            return previous_calendar_week()[0].isoformat()
        return scope

    def _own_item(self, claim: Claim, plan: list[CH.PlannedReport], quality) -> Item:
        rt, params = claim.report_type, claim.params
        scope = self._scope(rt, params)
        stored = (J.get_job(self.conn, claim.job_id) or {}).get("result") or {}
        if claim.trigger == "admin_force":
            return Item(rt, params, "admin_force", scope=scope)
        if claim.trigger == "source_change" and stored.get("plan"):
            p = stored["plan"]
            return Item(rt, params, p.get("cause") or "coverage_refresh", bool(p.get("announce")),
                        list(p.get("change_ids") or []), planned=True, scope=scope)
        match = next((p for p in plan if (p.report_type, p.scope) == (rt, scope)), None)
        if match is not None:
            # New data this report has not yet answered. Produced now, it is the edition that data
            # warrants and is announced as such, provided the rules a scheduled run would apply hold.
            ready = self.engine.readiness(rt, params, self._today(), quality)
            if ready is None or ready.get("ok"):
                return Item(rt, params, match.cause if match.announce else "coverage_refresh", match.announce,
                            list(match.change_ids), list(match.reasons), planned=True, scope=scope)
        return Item(rt, params, "manual_request", scope=scope)

    @staticmethod
    def _planned_params(p: CH.PlannedReport) -> dict[str, Any]:
        raw = {k: v for k, v in p.params.items() if k in PR.ALLOWED_KEYS}
        raw["refresh"] = "latest_data"
        return PR.normalise(p.report_type, raw)

    def _run_child(self, parent: Claim, item: Item) -> dict[str, Any]:
        job_id, created = J.create_job(
            self.conn, kind="report", report_type=item.report_type, params=item.params, trigger="source_change",
            environment=parent.environment, requested_by="scheduler", parent_job_id=parent.job_id,
            status="running", worker_id=self.worker_id, dispatch=False)
        if not created:
            # An equivalent job is already queued or running — someone asked for the same report.
            # It will see the same pending changes and answer them itself.
            return {"report_type": item.report_type, "scope": PR.scope_of(item.report_type, item.params),
                    "status": "deferred", "job_id": job_id, "note": "an equivalent job is already active"}
        with self.conn.cursor() as cur:
            from psycopg.types.json import Jsonb

            cur.execute("UPDATE report_jobs SET result = result || %s WHERE job_id = %s",
                        (Jsonb({"plan": {"cause": item.cause, "announce": item.announce,
                                         "change_ids": item.change_ids, "reasons": item.reasons[:10]}}), job_id))
        child = J.adopt(self.conn, job_id, worker_id=self.worker_id)
        self.heartbeat.add(child)
        try:
            return self._produce(child, item)
        except (LeaseLost,):
            return {"job_id": job_id, "report_type": item.report_type, "status": "lease_lost"}
        except JobCancelled:
            self._finish_quietly(child, "cancelled", error_code="cancelled", error_message="Cancelled.")
            return {"job_id": job_id, "report_type": item.report_type, "status": "cancelled"}
        except Outcome as o:
            self._finish_quietly(child, o.status, result=o.result, edition_id=o.edition_id,
                                 error_code=None if o.status in ("reused", "unchanged") else o.code,
                                 error_message=o.message)
            return {"job_id": job_id, "report_type": item.report_type, "status": o.status, "code": o.code}
        except Exception as exc:
            t = _transient(exc)
            detail = traceback.format_exc()
            if t is not None:
                try:
                    what = J.retry_later(self.conn, child, error_code=t.code, error_message=t.message,
                                         error_detail=detail)
                except LeaseLost:
                    what = "lease_lost"
                return {"job_id": job_id, "report_type": item.report_type, "status": what, "code": t.code}
            log.exception("child %s failed", job_id)
            self._finish_quietly(child, "failed", error_code="engine_error",
                                 error_message=f"The report engine stopped with {type(exc).__name__}.",
                                 error_detail=detail)
            return {"job_id": job_id, "report_type": item.report_type, "status": "failed", "code": "engine_error"}
        finally:
            self.heartbeat.remove(child)

    # ------------------------------------------------------------------ one report
    def _produce_own(self, claim: Claim, item: Item) -> dict[str, Any]:
        """The job's own report. A non-publication answer ends the job here, and the rest of the
        work — the other reports a source check planned — still runs."""
        try:
            return self._produce(claim, item)
        except Outcome as o:
            self._finish_quietly(claim, o.status, result=o.result, edition_id=o.edition_id,
                                 error_code=None if o.status in ("reused", "unchanged") else o.code,
                                 error_message=o.message)
            return {"job_id": claim.job_id, "report_type": item.report_type, "scope": item.scope,
                    "status": o.status, "code": o.code, "message": o.message}

    def _engine_request(self, rt: str, params: dict[str, Any]) -> dict[str, Any]:
        """Turn a validated request into the engine's arguments, or say why it cannot be answered."""
        period = params.get("period", "latest")
        if rt in ("monthly", "sector"):
            latest = self.engine.banking_month()
            if latest is None:
                raise Outcome("waiting_for_data", "missing_source",
                              "The banking tables this report is built on have not been collected yet.")
            if period != "latest" and period != latest:
                if period > latest:
                    raise Outcome("waiting_for_data", "not_yet_published",
                                  f"Banking data for {period} has not been published yet; the latest "
                                  f"available month is {latest}.", result={"latest_available": latest})
                edition = period if rt == "monthly" else f"{params['sector']}_{period}"
                existing = self._published(rt, edition)
                if existing:
                    return {"reuse": existing}
                raise Outcome("failed", "unsupported_period",
                              f"No {PR.REPORT_TYPES[rt]['label']} was published for {period}. Reports are "
                              f"built from the current information set, whose latest month is {latest}; an "
                              f"earlier month exists only as an archived edition, and there is none for {period}.",
                              result={"latest_available": latest})
            return {"sector": params.get("sector")}
        if rt == "weekly":
            from ..scheduling.tasks import previous_calendar_week

            if period == "latest":
                start, end = previous_calendar_week()
            else:
                start = dt.date.fromisoformat(period)
                end = start + dt.timedelta(days=6)
                if end >= self._today():
                    raise Outcome("failed", "unsupported_period",
                                  f"The week of {start.isoformat()} has not ended yet; a digest covers a "
                                  f"complete Monday-to-Sunday week.")
            return {"since": start.isoformat(), "until": end.isoformat()}
        pub_id = params.get("publication_id") or "latest"
        pub = self.engine.resolve_publication(rt, pub_id)
        if pub is None:
            label = PR.REPORT_TYPES[rt]["label"]
            raise Outcome("failed", "missing_source",
                          f"No source publication for a {label} has been collected" if pub_id == "latest"
                          else f"The publication {pub_id} has not been collected, so there is nothing to brief.")
        return {"publication_id": pub["publication_id"]}

    def _published(self, rt: str, edition: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT edition_id FROM publication_records WHERE report_type = %s AND edition = %s "
                        "ORDER BY version DESC LIMIT 1", (rt, edition))
            row = cur.fetchone()
        return row[0] if row else None

    def _produce(self, claim: Claim, item: Item) -> dict[str, Any]:
        rt, params = item.report_type, item.params
        scope = item.scope or self._scope(rt, params)
        base = {"job_id": claim.job_id, "report_type": rt, "scope": scope}
        request = self._engine_request(rt, params)
        if "reuse" in request:
            J.finish(self.conn, claim, "reused", edition_id=request["reuse"],
                     result={"message": "an archived edition for this period already exists"})
            return {**base, "status": "reused", "edition_id": request["reuse"]}

        force = item.cause == "admin_force"
        with self._progress(claim):
            J.set_stage(self.conn, claim, "calculating", None)
            result = self.engine.generate(rt, request, force=force)
            status = result.get("status")
            if status == "unchanged":
                existing = result.get("existing_edition_id")
                if existing and self._is_published(existing):
                    J.record_fingerprint(self.conn, environment=claim.environment, report_type=rt,
                                         scope_key=scope, fingerprint=str(result.get("fingerprint") or ""),
                                         edition_id=existing)
                    final = "unchanged" if item.planned or claim.trigger == "schedule" else "reused"
                    J.finish(self.conn, claim, final, edition_id=existing,
                             result={"message": "every input is unchanged since this edition was published",
                                     "fingerprint": result.get("fingerprint")})
                    return {**base, "status": final, "edition_id": existing}
                # The engine recognised an edition that was generated but never published — a worker
                # that died after rendering. It is not an answer; produce and publish a new version.
                J.note(self.conn, claim, "the matching edition was never published; producing it again",
                       {"unpublished": existing})
                result = self.engine.generate(rt, request, force=True)
                status = result.get("status")
            if status == "blocked":
                raise Outcome("blocked", "quality_blocked",
                              "Publication was blocked: " + str(result.get("cause") or "a blocking check failed")
                              + ". The previous edition remains current.",
                              result={"failed_checks": (result.get("failed_checks") or [])[:10]})
            if status == "no_publication":
                raise Outcome("failed", "missing_source", str(result.get("note") or "No source publication is held."))
            if status != "generated":
                raise Outcome("failed", "unexpected_result", f"The engine answered {status!r}.")
            self._dataset_dirty = True

            J.set_stage(self.conn, claim, "validating", None)
            try:
                checks = O.validate(rt, result, profile_allows_upload=bool(config.profile().get("external_upload")))
            except O.ValidationFailed as exc:
                self.engine.mark_edition(result["edition_id"], "blocked")
                raise Outcome("blocked", "validation_failed", f"Publication was blocked by validation: {exc}",
                              result={"checks": exc.checks, "unpublished_edition": result.get("edition_id")}) from None

            row = self.engine.edition_row(result["edition_id"]) or {}
            try:
                import json as _json

                fp_detail = _json.loads(row.get("fingerprint_detail") or "null")
            except ValueError:
                fp_detail = None
            version = int(result.get("version"))
            edition = (result.get("edition_key") if rt in ("mpr_brief", "fsr_brief", "decision_update")
                       else result.get("edition"))
            prefix = f"reports/{rt}/{edition}/v{version}/{claim.job_id}-a{claim.attempt}"

            J.set_stage(self.conn, claim, "uploading", f"{len(O.files_to_upload(Path(result['path'])))} file(s)")
            self._renew_dataset_lease(force=True)
            files = O.upload(self.store, Path(result["path"]), prefix, fence=_ClaimFence(claim))

        J.set_stage(self.conn, claim, "publishing", None)
        record = O.build_record(report_type=rt, result=result, params=params, files=files, checks=checks,
                                prefix=prefix, fingerprint_detail=fp_detail, scope_key=scope)
        entry = O.catalog_entry(record)
        summary = P.commit(self.conn, claim, record=record, catalog_entry=entry, cause=item.cause,
                           change_ids=item.change_ids)
        try:
            from ..cloud import objectstore as OS

            OS.write_catalog_entry(entry, self.store)
        except Exception:
            log.exception("the catalogue object for %s could not be written; the publication stands",
                          record["edition_id"])
        self._produced.append(record)
        return {**base, "status": "published", "edition_id": record["edition_id"], "cause": item.cause,
                "notifications": summary.get("notifications")}

    def _is_published(self, edition_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM publication_records WHERE edition_id = %s", (edition_id,))
            return cur.fetchone() is not None

    # ------------------------------------------------------------------ closing the loop
    def _close_changes(self, plan: list[CH.PlannedReport], items: list[tuple[Item, Claim | None]],
                       results: list[dict[str, Any]], waiting: list[dict[str, Any]]) -> None:
        """Close a change once every report it affects has answered it.

        A report that published or found its inputs unchanged has answered. One that is waiting for
        data, blocked, failed or deferred to another job has not, and the change stays pending so
        the next check plans it again.
        """
        answered: dict[tuple[str, str], str] = {}
        for (item, _), res in zip(items, results):
            answered[(item.report_type, item.scope)] = res.get("status") or ""
        by_change: dict[str, list[tuple[str, str]]] = {}
        for p in plan:
            for cid in p.change_ids:
                by_change.setdefault(cid, []).append((p.report_type, p.scope))
        done_published, done_unchanged = [], []
        for cid, keys in by_change.items():
            states = [answered.get(k) for k in keys]
            if all(s in ("published", "unchanged", "reused") for s in states):
                (done_published if "published" in states else done_unchanged).append(cid)
        CH.mark_handled(self.conn, done_published, job_id=None, handling="published")
        CH.mark_handled(self.conn, done_unchanged, job_id=None, handling="unchanged")


class _ClaimFence:
    """The storage layer's fence protocol, answered by the job lease."""

    def __init__(self, claim: Claim):
        self.claim = claim

    def check(self, what: str) -> None:
        self.claim.check(what)
