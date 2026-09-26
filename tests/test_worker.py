"""The job worker, end to end against a real Postgres and a real (local) object store.

The engine is replaced by one that writes genuine artefacts — a PPTX python-pptx can open, a PDF
pdfplumber can read, an XLSX openpyxl wrote — so the worker's validation, upload, verification and
publication run exactly as they do in production. What the stand-in does not do is compute
anything, which is what lets these tests control whether the inputs changed.

Everything is local integration: Postgres 16 in a schema per test, a directory as the object store.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from azmonitor.appstate import jobs as J
from azmonitor.appstate import publication as P
from azmonitor.util import progress

from appstate_fixtures import add_recipient, connect_in, pg, settings  # noqa: F401
from conftest import minimal_pdf

TODAY = dt.date(2026, 9, 26)


class FakeEngine:
    """Same methods as azmonitor.jobs.engine.Engine; real files, controlled inputs."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.inputs: dict[tuple[str, str], str] = {}     # (report, scope) -> a stand-in for the data
        self.banking = "2026-07"
        self.editions: dict[str, dict[str, Any]] = {}     # the dataset's report_editions table
        self.refresh_result: dict[str, Any] = {"datasets": {}}
        self.ready: dict[str, dict[str, Any]] = {}
        self.no_pdf = False
        self.blocked = False
        self.fail: BaseException | None = None
        self.during_generate = None
        self.generated: list[tuple[str, dict[str, Any], bool]] = []
        self.refreshes = 0
        self.publications_held = {"decision_update": [{"publication_id": "policy_decision:5531",
                                                       "published_at": "2026-07-31"}]}

    def open(self): pass
    def close(self): pass

    def refresh(self):
        self.refreshes += 1
        progress.detail("dataset 1 of 1: fake")
        return self.refresh_result

    def validate(self):
        return {"summary": {"critical": 0}}

    def banking_month(self):
        return self.banking

    def availability(self, today):
        return {"monthly": {"edition_month": self.banking}}

    def readiness(self, report_type, params, today, quality):
        return self.ready.get(report_type)

    def resolve_publication(self, report_type, publication_id):
        pubs = self.publications_held.get(report_type) or []
        if publication_id == "latest":
            return pubs[-1] if pubs else None
        return next((p for p in pubs if p["publication_id"] == publication_id), None)

    def edition_row(self, edition_id):
        return self.editions.get(edition_id)

    def mark_edition(self, edition_id, status):
        self.editions[edition_id]["status"] = status

    def sync_published(self, rows):
        added = 0
        for r in rows:
            if r["edition_id"] not in self.editions:
                self.editions[r["edition_id"]] = {"edition_id": r["edition_id"], "report_type": r["report_type"],
                                                  "edition_period": r["edition"], "version": r["version"],
                                                  "status": "generated", "fingerprint": r["fingerprint"],
                                                  "fingerprint_detail": json.dumps({"fingerprint": r["fingerprint"]})}
                added += 1
        return added

    def generate(self, report_type, request, *, force):
        self.generated.append((report_type, request, force))
        if self.during_generate:
            hook, self.during_generate = self.during_generate, None
            hook()
        if self.fail:
            raise self.fail
        if self.blocked:
            return {"status": "blocked", "cause": "critical data-quality checks failed", "failed_checks": [{"id": "x"}]}
        scope = request.get("sector") or request.get("publication_id") or request.get("since") or "latest"
        edition = {"monthly": self.banking, "sector": f"{request.get('sector')}_{self.banking}",
                   "weekly": f"week-{request.get('since')}"}.get(report_type) or str(request.get("publication_id")).replace(":", "_")
        fingerprint = f"{report_type}|{scope}|{self.inputs.get((report_type, scope), 'v1')}"
        prior = [e for e in self.editions.values() if e["report_type"] == report_type and e["edition_period"] == edition]
        live = [e for e in prior if e["status"] == "generated"]
        if live and not force and live[-1]["fingerprint"] == fingerprint:
            return {"status": "unchanged", "edition": edition, "existing_edition_id": live[-1]["edition_id"],
                    "version": live[-1]["version"], "fingerprint": fingerprint}
        progress.stage("calculating", "fake fact pack")
        version = max([e["version"] for e in prior], default=0) + 1
        progress.stage("writing_narrative", None)
        out = self.out_dir / report_type / edition / f"v{version}"
        out.mkdir(parents=True, exist_ok=True)
        progress.stage("rendering", "slides")
        from pptx import Presentation

        pptx = out / "deck.pptx"
        Presentation().save(pptx)
        pdf_info: dict[str, Any] = {"status": "skipped", "reason": "LibreOffice not available"}
        if not self.no_pdf:
            minimal_pdf(out / "deck.pdf", text="Azerbaijan monitor")
            pdf_info = {"status": "ok", "path": str(out / "deck.pdf")}
        import openpyxl

        wb = openpyxl.Workbook()
        for i in range(200):
            wb.active.append([f"row {i}", i, i * 1.5, "evidence"])
        wb.save(out / "evidence.xlsx")
        manifest = {"report_type": report_type, "edition": edition, "version": version,
                    "generated_at": "2026-09-26T09:20:00Z", "as_of": "2026-09-26", "n_slides": 1,
                    "narrative_mode": "facts_only", "narrative_validation": {"numbers_checked": 12, "problems": []},
                    "reporting_periods": {"banking_period": "2026-07-31", "cpi_period": "2026-08-31"},
                    "quality_summary": {"checks": 40, "failed": 0, "critical": 0},
                    "edition_fingerprint": {"fingerprint": fingerprint}, "language": "en"}
        (out / "manifest.json").write_text(json.dumps(manifest))
        (out / "narrative.json").write_text(json.dumps({"findings": [
            {"id": "f1", "statement": "Loans to households grew 18.2% y/y in July 2026.", "classification": "observed_fact"},
            {"id": "f2", "statement": "The NPL ratio of the banking sector was 2.9% at end-July.", "classification": "observed_fact"},
            {"id": "f3", "statement": "Deposits including financial corporations rose 6.1% y/y.", "classification": "observed_fact"}]}))
        (out / "fact_pack.json").write_text(json.dumps({"fact_pack_hash": fingerprint}))
        edition_id = f"{report_type}:{edition}:v{version}"
        self.editions[edition_id] = {"edition_id": edition_id, "report_type": report_type, "edition_period": edition,
                                     "version": version, "status": "generated", "fingerprint": fingerprint,
                                     "fingerprint_detail": json.dumps({"fingerprint": fingerprint})}
        res = {"status": "generated", "edition": edition, "version": version, "edition_id": edition_id,
               "fingerprint": fingerprint, "path": str(out), "pptx": str(pptx), "pdf": pdf_info,
               "xlsx": str(out / "evidence.xlsx"), "n_slides": 1}
        if report_type in ("mpr_brief", "fsr_brief", "decision_update"):
            res["edition_key"] = edition
        return res


@pytest.fixture
def world(pg, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AZMONITOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZMONITOR_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("AZMONITOR_PROFILE", "neutral")
    from azmonitor import config
    from azmonitor.cloud.objectstore import LocalObjectStore
    from azmonitor.storage.db import Database

    db = Database(config.paths().db_path)
    db.conn.execute("INSERT INTO documents(doc_id, source_id, dataset_id, document_url, sha256, retrieved_at, first_seen_at) "
                    "VALUES ('d1','cba','cba_deposits','https://example.az/x','abc','2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')")
    db.conn.commit()
    db.close()
    # The dataset lives in the store, as in production: every job restores it on a clean disk and
    # saves it back, so these tests run the fresh-runner path rather than a shortcut around it.
    from azmonitor.cloud import objectstore as OS

    store = LocalObjectStore(tmp_path / "store")
    OS.save_dataset(config.paths().data_dir, store)

    class World:
        conn = pg
        engine = FakeEngine(tmp_path / "outputs")

        def worker(self, **kw):
            from azmonitor.jobs.worker import Settings, Worker

            s = Settings(lease_seconds=60, heartbeat_interval=0.2, dataset_wait_seconds=kw.pop("wait", 2.0),
                         dataset_poll_seconds=0.2, skip_restore=False, save_dataset=True)
            return Worker(connect=lambda: connect_in(pg.schema), engine=kw.pop("engine", self.engine),
                          store=store, settings=s, worker=kw.pop("worker", "test-worker"), today=TODAY, **kw)

        def request(self, report_type="monthly", params=None, *, environment="production", trigger="manual",
                    notify=False, kind="report", slot=None):
            params = params or {"period": "latest", "refresh": "latest_data"}
            job_id, _ = J.create_job(pg, kind=kind, report_type=report_type, params=params, trigger=trigger,
                                     environment=environment, requested_by="owner",
                                     requester_email="owner@example.az" if notify else None,
                                     notify_requester=notify, schedule_slot=slot)
            return job_id

        def run(self, job_id, **kw):
            return self.worker(**kw).run(job_id)

        def job(self, job_id):
            return J.get_job(pg, job_id)

        def outbox(self):
            from psycopg.rows import dict_row

            with pg.cursor(row_factory=dict_row) as cur:
                return cur.execute("SELECT * FROM email_outbox ORDER BY created_at, delivery_id").fetchall()

        def changes(self, **recs):
            """Make the next refresh report these document-level changes."""
            self.engine.refresh_result = {"datasets": {dsid: {"changes": [r], "errors": []}
                                                       for dsid, r in recs.items()}}

    World.store = store
    return World()


def _stages(conn, job_id):
    return [e["stage"] for e in J.events(conn, job_id) if e["status"] == "running" and e["stage"]]


def _new_observations(dataset="cba_deposits", period="2026-08-31", latest_before="2026-07-31"):
    return {"dataset_id": dataset, "source_id": "cba", "document_id": f"{dataset}:doc-{period}",
            "new_publication": False, "published_at": "2026-09-25", "n_new": 30, "n_revisions": 0,
            "new_periods": [period], "revised_periods": [], "latest_before": latest_before, "language": "en"}


# ----------------------------------------------------------------------------- manual requests

def test_a_manual_request_is_produced_validated_uploaded_and_published(world):
    job_id = world.request(notify=True)
    out = world.run(job_id)
    job = world.job(job_id)
    assert out["claimed"] and job["status"] == "succeeded", (out, job["error_message"])
    assert job["edition_id"] == "monthly:2026-07:v1"
    assert _stages(world.conn, job_id)[:1] == ["starting"]
    for stage in ("calculating", "writing_narrative", "rendering", "validating", "uploading", "publishing"):
        assert stage in _stages(world.conn, job_id)
    assert job["stage"] == "complete" and job["lease_expires_at"] is None

    rec = P.published(world.conn, "monthly")[0]
    assert rec["cause"] == "manual_request"
    files = {f["role"]: f for f in rec["manifest"]["files"]}
    assert {"pdf", "pptx", "xlsx", "manifest"} <= set(files)
    for f in rec["manifest"]["files"]:
        assert f["key"].startswith(f"reports/monthly/2026-07/v1/{job_id}-a1/")
        assert world.store.stat(f["key"])["size"] == f["bytes"]
    assert rec["findings"][0].startswith("Loans to households")
    assert rec["reporting_periods"]["cpi_period"] == "2026-08-31"
    assert any("different periods" in lim for lim in rec["limitations"])
    # the requester, and only the requester, is emailed about a manual request
    rows = world.outbox()
    assert [(r["purpose"], r["to_address"], r["status"]) for r in rows] == [
        ("manual_request", "owner@example.az", "queued")]


def test_asking_again_with_unchanged_inputs_reuses_the_edition(world):
    first = world.request()
    world.run(first)
    again = world.request(notify=True)
    out = world.run(again)
    job = world.job(again)
    assert job["status"] == "reused" and job["edition_id"] == "monthly:2026-07:v1", out
    assert len(P.published(world.conn, "monthly")) == 1
    assert [r["purpose"] for r in world.outbox()] == ["manual_request"]
    assert world.outbox()[0]["edition_id"] == "monthly:2026-07:v1"


def test_a_forced_regeneration_makes_a_new_version_and_says_why(world):
    world.run(world.request())
    job_id, _ = J.create_job(world.conn, kind="report", report_type="monthly",
                             params={"period": "latest", "refresh": "latest_data"}, trigger="admin_force",
                             environment="production", requested_by="owner", force_reason="layout fix",
                             nonce="force-1")
    world.run(job_id)
    recs = P.published(world.conn, "monthly")
    assert [r["version"] for r in recs] == [1, 2] and recs[1]["cause"] == "admin_force"
    assert recs[1]["supersedes"] == "monthly:2026-07:v1"
    assert world.outbox() == []          # a forced regeneration announces nothing


def test_a_missing_pdf_blocks_publication_and_email(world):
    world.engine.no_pdf = True
    job_id = world.request(notify=True)
    world.run(job_id)
    job = world.job(job_id)
    assert job["status"] == "blocked" and job["error_code"] == "validation_failed"
    assert "no PDF" in job["error_message"]
    assert P.published(world.conn, "monthly") == [] and world.outbox() == []
    assert world.engine.editions["monthly:2026-07:v1"]["status"] == "blocked"
    # fixed, the next request produces a new version rather than reusing the blocked one
    world.engine.no_pdf = False
    again = world.request()
    world.run(again)
    assert world.job(again)["edition_id"] == "monthly:2026-07:v2"


def test_a_quality_failure_blocks_with_the_reason(world):
    world.engine.blocked = True
    job_id = world.request()
    world.run(job_id)
    job = world.job(job_id)
    assert job["status"] == "blocked" and job["error_code"] == "quality_blocked"
    assert "previous edition remains current" in job["error_message"]


def test_unsupported_and_future_periods_are_explained_not_attempted(world):
    past = world.request(params={"period": "2025-01", "refresh": "latest_data"})
    world.run(past)
    job = world.job(past)
    assert job["status"] == "failed" and job["error_code"] == "unsupported_period"
    assert "2025-01" in job["error_message"] and "2026-07" in job["error_message"]

    future = world.request(params={"period": "2026-09", "refresh": "latest_data"})
    world.run(future)
    assert world.job(future)["status"] == "waiting_for_data"
    assert world.engine.generated == []            # neither reached the engine

    week = world.request("weekly", {"period": "2026-09-21", "refresh": "latest_data"})
    world.run(week)
    assert world.job(week)["error_code"] == "unsupported_period"


def test_an_archived_period_is_offered_rather_than_rebuilt(world):
    world.run(world.request())                              # publishes 2026-07
    world.engine.banking = "2026-08"
    job_id = world.request(params={"period": "2026-07", "refresh": "latest_data"})
    world.run(job_id)
    job = world.job(job_id)
    assert job["status"] == "reused" and job["edition_id"] == "monthly:2026-07:v1"


def test_a_missing_source_publication_is_a_clear_failure(world):
    job_id = world.request("mpr_brief", {"publication_id": "latest", "refresh": "latest_data"})
    world.run(job_id)
    job = world.job(job_id)
    assert job["status"] == "failed" and job["error_code"] == "missing_source"


def test_a_tampered_request_is_refused_before_the_engine_sees_it(world):
    job_id = world.request(params={"period": "latest", "refresh": "latest_data"})
    world.conn.execute("UPDATE report_jobs SET params = %s WHERE job_id = %s",
                       (json.dumps({"period": "latest", "refresh": "latest_data", "path": "/etc/passwd"}), job_id))
    world.run(job_id)
    job = world.job(job_id)
    assert job["status"] == "failed" and job["error_code"] == "unknown_parameter"
    assert world.engine.generated == []


# ----------------------------------------------------------------------------- automatic path

def test_new_official_data_publishes_an_edition_and_announces_it_once(world):
    settings(world.conn, "production", auto_email_enabled=True)
    add_recipient(world.conn, "owner@example.az", ("monthly",), role="owner")
    world.changes(cba_deposits=_new_observations())
    world.engine.inputs[("monthly", "latest")] = "august-data"
    parent = world.request(None, {}, kind="source_check", trigger="schedule", slot="source_check:2026-09-26T09:15")
    out = world.run(parent)
    assert world.job(parent)["status"] == "succeeded", out

    children = world.conn.execute("SELECT * FROM report_jobs WHERE parent_job_id = %s", (parent,)).fetchall()
    assert len(children) >= 1
    monthly = [c for c in children if c[2] == "monthly"]
    rec = P.published(world.conn, "monthly")[0]
    assert rec["cause"] == "new_data"
    rows = world.outbox()
    assert [(r["purpose"], r["to_address"], r["status"]) for r in rows if r["edition_id"] == rec["edition_id"]] == [
        ("new_edition", "owner@example.az", "queued")]
    assert monthly

    # the change is answered, so the next check with nothing new produces nothing and emails nobody
    world.changes()
    n_emails = len(world.outbox())
    second = world.request(None, {}, kind="source_check", trigger="schedule", slot="source_check:2026-09-26T13:15")
    world.run(second)
    assert world.job(second)["status"] == "unchanged"
    assert "nothing was published" in world.job(second)["result"]["message"]
    assert len(world.outbox()) == n_emails and len(P.published(world.conn, "monthly")) == 1


def test_a_correction_becomes_a_revision_not_a_new_release(world):
    settings(world.conn, "production", auto_email_enabled=True, revision_settle_minutes=0)
    add_recipient(world.conn, "owner@example.az", ("monthly",), role="owner")
    world.changes(cba_deposits=_new_observations())
    world.engine.inputs[("monthly", "latest")] = "august"
    world.run(world.request(None, {}, kind="source_check", trigger="schedule", slot="s1"))
    world.conn.execute("UPDATE email_outbox SET status = 'accepted'")      # the first announcement went out

    world.changes(cba_deposits={**_new_observations(), "document_id": "cba_deposits:corrected",
                                "new_periods": [], "n_new": 0, "n_revisions": 4,
                                "revised_periods": ["2026-07-31"], "latest_before": "2026-08-31"})
    world.engine.inputs[("monthly", "latest")] = "august-corrected"
    world.run(world.request(None, {}, kind="source_check", trigger="schedule", slot="s2"))
    recs = P.published(world.conn, "monthly")
    assert [r["version"] for r in recs] == [1, 2] and recs[1]["cause"] == "revision"
    purposes = [r["purpose"] for r in world.outbox()]
    assert purposes == ["new_edition", "revised_edition"]


def test_translations_and_backfills_are_recorded_and_announce_nothing(world):
    settings(world.conn, "production", auto_email_enabled=True)
    add_recipient(world.conn, "owner@example.az", ("monthly", "mpr_brief"), role="owner")
    world.changes(
        cba_monetary_policy_review={"dataset_id": "cba_monetary_policy_review", "source_id": "cba",
                                    "document_id": "mpr:en", "publication_id": "monetary_policy_review:2026-08",
                                    "pub_type": "monetary_policy_review", "is_translation": True, "language": "en",
                                    "original_language": "az", "new_publication": False, "n_new": 0, "n_revisions": 0},
        cba_deposits={**_new_observations(period="2019-01-31", latest_before="2026-07-31")})
    parent = world.request(None, {}, kind="source_check", trigger="schedule", slot="s1")
    world.run(parent)
    assert world.job(parent)["status"] == "unchanged"
    assert world.engine.generated == [] and world.outbox() == []
    rows = world.conn.execute("SELECT classification, handling FROM source_changes ORDER BY classification").fetchall()
    assert rows == [("historical_backfill", "not_productive"), ("translation", "not_productive")]


def test_a_monthly_waiting_for_its_tables_keeps_the_change_until_it_can_answer(world):
    settings(world.conn, "production", auto_email_enabled=True)
    add_recipient(world.conn, "owner@example.az", ("monthly",), role="owner")
    world.changes(cba_deposits=_new_observations())
    world.engine.ready["monthly"] = {"ok": False, "state": "waiting",
                                     "reasons": [{"rule": "companion_datasets", "met": False}], "missing": ["cba_bank_npl"]}
    world.engine.ready["sector"] = {"ok": False, "state": "waiting", "reasons": [], "missing": []}
    first = world.request(None, {}, kind="source_check", trigger="schedule", slot="s1")
    world.run(first)
    result = world.job(first)["result"]
    assert world.job(first)["status"] == "unchanged"
    assert {w["report_type"] for w in result["waiting"]} >= {"monthly"}
    assert world.conn.execute("SELECT count(*) FROM source_changes WHERE handled_at IS NULL").fetchone()[0] == 1

    world.changes()                                     # nothing new arrives; the companion table does
    world.engine.ready.clear()
    world.engine.inputs[("monthly", "latest")] = "august"
    second = world.request(None, {}, kind="source_check", trigger="schedule", slot="s2")
    world.run(second)
    assert world.job(second)["status"] == "succeeded"
    assert P.published(world.conn, "monthly")[0]["cause"] == "new_data"
    assert world.conn.execute("SELECT count(*) FROM source_changes WHERE handled_at IS NULL").fetchone()[0] == 0


def test_a_manual_source_check_answers_the_change_and_announces_to_subscribers(world):
    settings(world.conn, "production", auto_email_enabled=True)
    add_recipient(world.conn, "owner@example.az", ("monthly",), role="owner")
    world.changes(cba_deposits=_new_observations())
    world.engine.inputs[("monthly", "latest")] = "august"
    job_id = world.request(params={"period": "latest", "refresh": "check_sources"}, notify=True)
    world.run(job_id)
    job = world.job(job_id)
    assert job["status"] == "succeeded" and world.engine.refreshes == 1
    rec = P.published(world.conn, "monthly")[0]
    assert rec["cause"] == "new_data"
    # the requester is a subscriber: one message, the announcement, not two
    assert [(r["purpose"], r["to_address"]) for r in world.outbox() if r["edition_id"] == rec["edition_id"]] == [
        ("new_edition", "owner@example.az")]


def test_preview_deployments_record_announcements_but_never_queue_them(world):
    settings(world.conn, "preview", auto_email_enabled=True)
    add_recipient(world.conn, "owner@example.az", ("monthly",), role="owner")
    world.changes(cba_deposits=_new_observations())
    world.engine.inputs[("monthly", "latest")] = "august"
    world.run(world.request(None, {}, kind="source_check", trigger="schedule", slot="p1", environment="preview"))
    rows = world.outbox()
    assert rows and all(r["status"] == "suppressed" for r in rows)
    assert "preview" in rows[0]["status_reason"]


# ----------------------------------------------------------------------------- recovery

def test_a_worker_that_lost_its_lease_cannot_publish(world):
    job_id = world.request()

    def taken_over():
        world.conn.execute("UPDATE report_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s",
                           (job_id,))
        J.reap(world.conn, "production", unclaimed_grace_seconds=0)
        world.conn.execute("UPDATE report_jobs SET next_attempt_at = NULL WHERE job_id = %s", (job_id,))
        assert J.claim(world.conn, job_id, worker_id="newer-worker") is not None

    world.engine.during_generate = taken_over
    out = world.run(job_id)
    assert "lease_lost" in out
    assert P.published(world.conn, "monthly") == []
    job = world.job(job_id)
    assert job["status"] == "running" and job["worker_id"] == "newer-worker" and job["attempt"] == 2


def test_storage_trouble_is_retried_later_with_backoff(world):
    from azmonitor.cloud.objectstore import StorageError

    world.engine.fail = StorageError("blob store returned 503")
    job_id = world.request()
    out = world.run(job_id)
    job = world.job(job_id)
    assert out["status"] == "requeued" and job["status"] == "queued"
    assert job["error_code"] == "storage_unavailable" and job["next_attempt_at"] is not None
    pending = world.conn.execute("SELECT attempt, status FROM job_dispatches WHERE job_id = %s AND status = 'pending'",
                                 (job_id,)).fetchall()
    assert pending == [(2, "pending")]


def test_an_engine_error_fails_the_job_instead_of_leaving_it_running(world):
    world.engine.fail = KeyError("anchors")
    job_id = world.request()
    world.run(job_id)
    job = world.job(job_id)
    assert job["status"] == "failed" and job["error_code"] == "engine_error"
    assert "KeyError" in job["error_message"] and "Traceback" in job["error_detail"]
    assert job["lease_expires_at"] is None


def test_a_busy_dataset_is_waited_for_then_retried(world):
    from azmonitor.cloud import lock as L

    other = connect_in(world.conn.schema)
    L.DatabaseLease(other, "azmonitor-run", holder="someone-else").try_acquire()
    job_id = world.request()
    out = world.run(job_id, wait=0.5)
    assert out["status"] == "requeued" and world.job(job_id)["error_code"] == "dataset_busy"
    other.close()


def test_cancellation_is_honoured_at_the_next_stage(world):
    job_id = world.request()

    def cancel():
        world.conn.execute("UPDATE report_jobs SET cancel_requested = true WHERE job_id = %s", (job_id,))

    world.engine.during_generate = cancel
    world.run(job_id)
    assert world.job(job_id)["status"] == "cancelled"
    assert P.published(world.conn, "monthly") == []


def test_editions_published_by_a_worker_whose_dataset_save_failed_are_restored(world):
    world.run(world.request())
    # a fresh runner whose dataset predates that publication
    from azmonitor.jobs.worker import Worker  # noqa: F401

    fresh = FakeEngine(world.engine.out_dir)
    fresh.inputs[("monthly", "latest")] = "august"
    job_id = world.request()
    world.run(job_id, engine=fresh)
    assert world.job(job_id)["edition_id"] == "monthly:2026-07:v2"          # not a second v1
    assert [r["version"] for r in P.published(world.conn, "monthly")] == [1, 2]


def test_two_workers_on_overlapping_jobs_take_turns_on_the_dataset(world):
    a = world.request("monthly")
    b = world.request("sector", {"period": "latest", "refresh": "latest_data", "sector": "agriculture"})
    results = {}

    def go(job_id, name):
        results[name] = world.run(job_id, worker=name, wait=30.0)

    ta = threading.Thread(target=go, args=(a, "wa"))
    tb = threading.Thread(target=go, args=(b, "wb"))
    ta.start(); tb.start(); ta.join(60); tb.join(60)
    assert world.job(a)["status"] == "succeeded" and world.job(b)["status"] == "succeeded", results
    published = sorted(r[0] for r in world.conn.execute("SELECT report_type FROM publication_records").fetchall())
    assert published == ["monthly", "sector"]
    # one held the dataset while the other waited: their dataset work never overlapped
    spans = []
    for j in (a, b):
        ev = J.events(world.conn, j)
        start = next(e["at"] for e in ev if e["stage"] == "restoring")
        end = next(e["at"] for e in ev if e["status"] == "succeeded")
        spans.append((start, end))
    spans.sort()
    assert spans[0][1] <= spans[1][0]


def test_a_production_job_refuses_to_run_with_test_hooks_set(world, monkeypatch):
    monkeypatch.setenv("AZMONITOR_REFRESH_DATASETS", "cba_deposits")
    job_id = world.request()
    world.run(job_id)
    job = world.job(job_id)
    assert job["status"] == "failed" and job["error_code"] == "misconfigured"
    assert "AZMONITOR_REFRESH_DATASETS" in job["error_message"]
    assert world.engine.generated == []


def test_each_attempt_works_in_its_own_directory_and_leaves_nothing_behind(world):
    """A replaced worker must never share a dataset file with its successor (see _own_workspace)."""
    import os

    from azmonitor import config

    base = config.paths().data_dir
    seen = {}

    def look():
        seen["data"] = os.environ["AZMONITOR_DATA_DIR"]

    world.engine.during_generate = look
    world.run(world.request())
    assert seen["data"] != str(base) and f"{base.name}.jobs" in seen["data"]
    assert os.environ["AZMONITOR_DATA_DIR"] == str(base)            # restored for whatever runs next
    assert list((base.parent / f"{base.name}.jobs").iterdir()) == []  # and removed
