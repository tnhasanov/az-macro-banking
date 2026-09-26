"""The two user journeys, end to end, on this machine.

    python scripts/e2e/run.py            # everything, about 40 minutes
    python scripts/e2e/run.py --only J5  # one journey and what it depends on

What is real here: the reporting engine (fact pack, narrative validation, python-pptx, LibreOffice
PDF), the official CBA website (the deposits table is downloaded live), Postgres 16, the Next.js
production build serving the dashboard, a Chromium browser driven by Playwright, the job worker,
leases, heartbeats, the reaper, the dispatch outbox and the email ledger.

What is substituted, and labelled as such in the report: private Blob storage is a local
directory (the same ObjectStore interface the tests use), GitHub Actions is the web application's
local dispatcher (it runs the same `python -m azmonitor.jobs run --job-id` a runner would), and
email goes to a capture directory instead of Resend. The environment is "test", which is never a
Vercel environment, and every test hook the worker honours is refused for a production job.

The run needs a dedicated Postgres database (default: azmon_e2e on the local test server). It is
dropped and recreated; nothing else is touched.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / ".e2e"
DB_URL = os.environ.get("AZMONITOR_E2E_DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/azmon_e2e")
PORT, PREVIEW_PORT = 3100, 3101
BASE = f"http://localhost:{PORT}"
PASSPHRASE = "local end-to-end passphrase, not a real credential"
OWNER = "owner@e2e.test"
CRON_SECRET = secrets.token_hex(24)
SESSION_SECRET = secrets.token_hex(32)

sys.path.insert(0, str(ROOT))


def pbkdf2(passphrase: str, iterations: int = 600_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, iterations, 32)
    return f"pbkdf2${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def env_common() -> dict[str, str]:
    return {
        "AZMONITOR_DATABASE_URL": DB_URL, "AZMONITOR_ENVIRONMENT": "test", "AZMONITOR_PROFILE": "neutral",
        "AZMONITOR_OBJECT_STORE_DIR": str(E2E / "store"), "AZMONITOR_DATA_DIR": str(E2E / "data"),
        "AZMONITOR_OUTPUT_DIR": str(E2E / "outputs"), "AZMONITOR_TEST_NOTIFICATIONS": "1",
        "AZMONITOR_REFRESH_DATASETS": "cba_deposits", "AZMONITOR_JOB_LEASE_SECONDS": "45",
        "AZMONITOR_HEARTBEAT_SECONDS": "5", "AZMONITOR_FETCH_OVERRIDES": str(E2E / "overrides.json"),
        "AZMONITOR_DATASET_WAIT_SECONDS": "1500",
    }


def env_web(port: int, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, **env_common(),
           "AZMONITOR_DISPATCH_MODE": "local", "AZMONITOR_PYTHON": sys.executable,
           "AZMONITOR_EMAIL_PROVIDER": "capture", "AZMONITOR_EMAIL_CAPTURE_DIR": str(E2E / "mail"),
           "AZMONITOR_SESSION_SECRET": SESSION_SECRET, "AZMONITOR_DASHBOARD_PASSPHRASE_HASH": pbkdf2(PASSPHRASE),
           "AZMONITOR_OWNER_EMAIL": OWNER, "CRON_SECRET": CRON_SECRET,
           "AZMONITOR_APP_URL": f"http://localhost:{port}", "PORT": str(port)}
    env.update(extra or {})
    return env


# ------------------------------------------------------------------ plumbing

def db():
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(DB_URL, autocommit=True, row_factory=dict_row)
    return conn


def q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with db() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def http(method: str, path: str, *, body: Any = None, headers: dict[str, str] | None = None,
         base: str = BASE) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        **({"content-type": "application/json"} if body is not None else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")[:200]


def tick(base: str = BASE) -> dict[str, Any]:
    status, body = http("GET", "/api/cron/tick", headers={"authorization": f"Bearer {CRON_SECRET}"}, base=base)
    assert status == 200, (status, body)
    return body


def wait_job(job_id: str, timeout: float = 1800, *, tick_every: float = 20) -> dict[str, Any]:
    terminal = ("succeeded", "reused", "unchanged", "waiting_for_data", "blocked", "failed", "cancelled")
    deadline = time.time() + timeout
    last_tick = 0.0
    while time.time() < deadline:
        row = q("SELECT * FROM report_jobs WHERE job_id = %s", (job_id,))[0]
        if row["status"] in terminal:
            return row
        if time.time() - last_tick > tick_every:
            tick()
            last_tick = time.time()
        time.sleep(3)
    raise TimeoutError(f"{job_id} did not finish within {timeout}s")


def run_worker(job_id: str, *, extra_env: dict[str, str] | None = None, background: bool = False):
    env = {**os.environ, **env_common(), **(extra_env or {})}
    log = open(E2E / "logs" / f"{job_id}.direct.log", "a")
    cmd = [sys.executable, "-m", "azmonitor.jobs", "run", "--job-id", job_id]
    if background:
        return subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=log)
    return subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=log, timeout=3600).returncode


def create_job(**kw) -> str:
    os.environ.update({"AZMONITOR_DATABASE_URL": DB_URL})
    from azmonitor.appstate import jobs as J

    with db() as conn:
        job_id, _ = J.create_job(conn, environment="test", requested_by=kw.pop("requested_by", "scheduler"), **kw)
    return job_id


def outbox() -> list[dict[str, Any]]:
    return q("SELECT delivery_id, purpose, to_address, status, status_reason, edition_id, attempts FROM email_outbox "
             "ORDER BY created_at")


def captured() -> list[dict[str, Any]]:
    out = []
    for f in sorted((E2E / "mail").glob("*.json"), key=lambda p: p.stat().st_mtime):
        out.append(json.loads(f.read_text()))
    return out


def edit_stored_dataset(edit) -> dict[str, Any]:
    """Change the dataset in the store the way a source would have: restore, edit, save a new pointer."""
    from azmonitor.cloud import objectstore as OS

    store = OS.LocalObjectStore(E2E / "store")
    work = E2E / "edit"
    shutil.rmtree(work, ignore_errors=True)
    OS.restore_dataset(work, store)
    conn = sqlite3.connect(work / "monitor.sqlite")
    detail = edit(conn)
    conn.commit()
    conn.close()
    OS.save_dataset(work, store)
    shutil.rmtree(work, ignore_errors=True)
    return detail or {}


def reprocess_deposits(conn) -> None:
    """Make the next check read the deposits table again, as it would a newly published file."""
    conn.execute("UPDATE documents SET status = 'superseded' WHERE dataset_id = 'cba_deposits' AND status = 'parsed'")


# ------------------------------------------------------------------ setup

def prepare() -> None:
    import psycopg

    shutil.rmtree(E2E, ignore_errors=True)
    for d in ("store", "data", "outputs", "mail", "logs", "evidence", "seed"):
        (E2E / d).mkdir(parents=True, exist_ok=True)
    (E2E / "overrides.json").write_text("{}")

    admin_url = DB_URL.rsplit("/", 1)[0] + "/postgres"
    name = DB_URL.rsplit("/", 1)[1]
    assert name.startswith("azmon_e2e"), "refusing to reset a database whose name is not azmon_e2e*"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {name}")
    os.environ["AZMONITOR_DATABASE_URL"] = DB_URL
    from azmonitor.appstate import migrate
    from azmonitor.cloud import readmodel as RM

    with db() as conn:
        RM.ensure_schema(conn)
        migrate(conn, applied_by="e2e")

    # The seed: the real dataset, with the July deposits table withdrawn, so that the live CBA
    # file is a genuine new release when the scheduled check downloads it.
    seed = E2E / "seed"
    shutil.copy2(ROOT / "data" / "monitor.sqlite", seed / "monitor.sqlite")
    shutil.copytree(ROOT / "data" / "raw", seed / "raw", copy_function=os.link)
    for part in ("deliveries.sqlite", "state"):
        src = ROOT / "data" / part
        if src.is_dir():
            shutil.copytree(src, seed / part)
        elif src.exists():
            shutil.copy2(src, seed / part)
    conn = sqlite3.connect(seed / "monitor.sqlite")
    conn.execute("DELETE FROM observations WHERE dataset_id = 'cba_deposits' AND period_end = '2026-07-31'")
    reprocess_deposits(conn)
    conn.execute("UPDATE dataset_state SET latest_period_end = '2026-06-30' WHERE dataset_id = 'cba_deposits'")
    conn.commit()
    conn.close()
    from azmonitor.cloud import objectstore as OS

    OS.save_dataset(seed, OS.LocalObjectStore(E2E / "store"))
    shutil.rmtree(seed)

    with db() as conn:
        from azmonitor.appstate import new_id

        rid = new_id("rcp")
        conn.execute("INSERT INTO recipients(recipient_id, email, role) VALUES (%s, %s, 'owner')", (rid, OWNER))
        for rt in ("monthly", "weekly", "sector", "mpr_brief", "fsr_brief", "decision_update"):
            conn.execute("INSERT INTO subscriptions(recipient_id, report_type) VALUES (%s, %s)", (rid, rt))
        conn.execute("INSERT INTO notification_settings(environment, auto_email_enabled, revision_settle_minutes, "
                     "updated_by) VALUES ('test', true, 0, 'e2e')")


def start_web(port: int, extra: dict[str, str] | None = None) -> subprocess.Popen:
    log = open(E2E / "logs" / f"web-{port}.log", "a")
    proc = subprocess.Popen(["npx", "next", "start", "-p", str(port)], cwd=ROOT / "web", env=env_web(port, extra),
                            stdout=log, stderr=log, start_new_session=True)
    for _ in range(90):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/login", timeout=5)
            return proc
        except Exception:
            time.sleep(1)
    raise RuntimeError(f"the web application did not start on port {port}")


def stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)
    except Exception:
        pass


def browser(step: str, base: str = BASE) -> dict[str, Any]:
    npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    out = subprocess.run(["node", str(ROOT / "scripts" / "e2e" / "browser.cjs"), step, base, str(E2E / "evidence")],
                         env={**os.environ, "NODE_PATH": npm_root, "E2E_PASSPHRASE": PASSPHRASE},
                         capture_output=True, text=True, timeout=3600)
    last = (out.stdout.strip().splitlines() or ["{}"])[-1]
    result = json.loads(last)
    if out.returncode != 0 or "error" in result:
        raise RuntimeError(f"browser step {step} failed: {result.get('error') or out.stderr[-2000:]}")
    return result


# ------------------------------------------------------------------ the journeys

REPORT: dict[str, dict[str, Any]] = {}


def record(key: str, title: str, kind: str, passed: bool, evidence: dict[str, Any]) -> None:
    REPORT[key] = {"title": title, "kind": kind, "passed": bool(passed), "evidence": evidence}
    print(f"[{'PASS' if passed else 'FAIL'}] {key} {title}", flush=True)


def j1_to_j4() -> None:
    r = browser("generate")
    job = q("SELECT * FROM report_jobs WHERE job_id = %s", (r["jobId"],))[0]
    pub = q("SELECT * FROM publication_records WHERE edition_id = %s", (job["edition_id"],))
    import pdfplumber

    with pdfplumber.open(r["pdfPath"]) as pdf:
        pages = len(pdf.pages)
        first_page = (pdf.pages[0].extract_text() or "")[:200]
    n_slides = pub[0]["manifest"]["n_slides"] if pub else None
    record("J1", "Generate a report in the browser and download the real PDF", "local integration (real engine + LibreOffice)",
           job["status"] == "succeeded" and pages == n_slides and pages > 5,
           {"job_id": r["jobId"], "edition_id": job["edition_id"], "pdf_pages": pages, "deck_slides": n_slides,
            "pdf_bytes": r["pdfBytes"], "pdf_first_page": first_page, "stages": r["finalStages"],
            "cause": pub[0]["cause"] if pub else None})
    record("J2", "Close the browser mid-run and come back to the same job", "local integration (browser)",
           r["listedAfterReturn"] and job["status"] == "succeeded",
           {"stages_when_left": r["firstStages"], "stages_on_return": r["midStages"], "outcome": r["outcome"]})
    n_jobs = q("SELECT count(*) AS n FROM report_jobs WHERE kind = 'report' AND report_type = 'monthly'")[0]["n"]
    record("J3", "A double click and a second tab start one job", "local integration (browser + Postgres)",
           r["secondRequestJobId"] == r["jobId"] and n_jobs == 1,
           {"job_id": r["jobId"], "second_request": {"job_id": r["secondRequestJobId"], "created": r["secondRequestCreated"]},
            "monthly_jobs_in_database": n_jobs})
    tick()                                   # anything not already sent while the page was watching
    mail = [m for m in captured() if m["to"] == OWNER]
    REPORT["J1"]["evidence"]["requester_email"] = {"sent": len(mail), "subject": mail[0]["subject"] if mail else None,
                                                   "attachment": mail[0]["attachment"] if mail else None}
    r4 = browser("reuse")
    job4 = q("SELECT * FROM report_jobs WHERE job_id = %s", (r4["jobId"],))[0]
    versions = q("SELECT count(*) AS n FROM publication_records WHERE report_type = 'monthly'")[0]["n"]
    record("J4", "An identical request is offered the existing edition and reuses it", "local integration (browser + real engine)",
           "identical validated edition" in r4["offerText"] and job4["status"] == "reused"
           and job4["edition_id"] == job["edition_id"] and versions == 1,
           {"offer": r4["offerText"][:300], "generate_anyway_job": r4["jobId"], "status": job4["status"],
            "edition_id": job4["edition_id"], "monthly_publications": versions})


def j13() -> None:
    checks = {}
    checks["api without a session"] = http("POST", "/api/jobs", body={"report_type": "monthly"})[0]
    checks["job page without a session"] = http("GET", "/api/jobs/job_" + "0" * 26)[0]
    checks["wrong passphrase"] = http("POST", "/api/auth/login", body={"passphrase": "guess"})[0]
    checks["scheduler without its secret"] = http("GET", "/api/cron/tick")[0]
    checks["settings without a session"] = http("POST", "/api/settings", body={"auto_email_enabled": True})[0]
    checks["file download without a session"] = http("GET", "/api/files/reports/monthly/x.pdf")[0]
    checks["unsigned webhook"] = http("POST", "/api/webhooks/resend", body={"type": "email.delivered"})[0]
    checks["forged unsubscribe"] = http("POST", "/api/unsubscribe?t=rcp_x.forged")[0]
    expected = {"api without a session": 401, "job page without a session": 401, "wrong passphrase": 401,
                "scheduler without its secret": 401, "settings without a session": 401,
                "file download without a session": 401, "unsigned webhook": 503, "forged unsubscribe": 400}
    record("J13", "Unauthorised access is rejected", "local integration (HTTP against the production build)",
           checks == expected, {"status_codes": checks, "expected": expected,
                                "note": "the webhook answers 503 here because this local run has no webhook "
                                        "secret; with one configured an unsigned call is 401 (web/tests/routes-db.test.ts)"})


def j5_to_j7() -> None:
    before = len(outbox())
    parent = create_job(kind="source_check", report_type=None, params={}, trigger="schedule",
                        schedule_slot="source_check:e2e-1")
    tick()
    done = wait_job(parent, 1800)
    tick()                                            # send what the check queued
    children = q("SELECT job_id, report_type, status, edition_id, result FROM report_jobs WHERE parent_job_id = %s", (parent,))
    changes = q("SELECT classification, dataset_id, periods, handling FROM source_changes ORDER BY detected_at")
    monthly = [c for c in children if c["report_type"] == "monthly"]
    pub = q("SELECT edition_id, cause, version, supersedes FROM publication_records WHERE edition_id = %s",
            (monthly[0]["edition_id"],)) if monthly and monthly[0]["edition_id"] else []
    record("J5", "A new official release is detected and a validated edition is published automatically",
           "local integration (live CBA website + real engine)",
           done["status"] == "succeeded" and pub and pub[0]["cause"] == "new_data",
           {"source_check": parent, "status": done["status"], "changes": changes, "children": [
               {k: c[k] for k in ("job_id", "report_type", "status", "edition_id")} for c in children],
            "publication": pub, "waiting": (done["result"] or {}).get("waiting")})
    rows = outbox()[before:]
    announcements = [r for r in rows if r["purpose"] in ("new_edition", "revised_edition")]
    mail = [m for m in captured() if pub and "2026-07" in m["subject"]]
    record("J6", "Exactly one notification is sent to the subscribed owner", "local integration (capture provider)",
           len(announcements) == 1 and announcements[0]["status"] == "accepted" and len(mail) == 1,
           {"outbox": rows, "captured_subject": mail[0]["subject"] if mail else None,
            "captured_text_head": mail[0]["text"][:900] if mail else None})
    n_pub, n_mail = q("SELECT count(*) AS n FROM publication_records")[0]["n"], len(outbox())
    again = create_job(kind="source_check", report_type=None, params={}, trigger="schedule",
                       schedule_slot="source_check:e2e-2")
    tick()
    done2 = wait_job(again, 1800)
    tick()
    record("J7", "A repeat check with nothing new produces no edition and no email", "local integration (live CBA website)",
           done2["status"] == "unchanged" and q("SELECT count(*) AS n FROM publication_records")[0]["n"] == n_pub
           and len(outbox()) == n_mail,
           {"source_check": again, "status": done2["status"], "message": (done2["result"] or {}).get("message")})


def j8() -> None:
    """The CBA republishes the deposits file with July corrected: the live file, with three cells changed."""
    import openpyxl

    row = q("SELECT 1")  # noqa: F841 - keep the connection warm
    from azmonitor.cloud import objectstore as OS

    work = E2E / "edit"
    shutil.rmtree(work, ignore_errors=True)
    OS.restore_dataset(work, OS.LocalObjectStore(E2E / "store"))
    conn = sqlite3.connect(work / "monitor.sqlite")
    doc = conn.execute("SELECT document_url, stored_path FROM documents WHERE dataset_id = 'cba_deposits' "
                       "AND status = 'parsed' ORDER BY retrieved_at DESC LIMIT 1").fetchone()
    conn.close()
    source = work / doc[1]
    corrected = E2E / "cba_deposits_corrected.xlsx"
    wb = openpyxl.load_workbook(source)
    ws = wb["2.11"]
    changed = []
    # Months are text ("07") under a row per year; the last "07" in the table is July 2026, the
    # latest July the file carries (the live file already runs to August 2026).
    july = [r for r in ws.iter_rows(min_row=7) if str(r[0].value or "").strip() == "07"][-1]
    for col in (1, 2, 3):                              # total, households, household AZN demand
        old = july[col].value
        july[col].value = round(float(old) + 250.0, 1)
        changed.append({"row": july[0].row, "column": col, "was": old, "now": july[col].value})
    wb.save(corrected)
    shutil.rmtree(work, ignore_errors=True)
    (E2E / "overrides.json").write_text(json.dumps({doc[0]: str(corrected)}))

    job = create_job(kind="source_check", report_type=None, params={}, trigger="schedule", schedule_slot="source_check:e2e-3")
    tick()
    done = wait_job(job, 1800)
    tick()
    pubs = q("SELECT edition_id, version, cause, supersedes FROM publication_records WHERE report_type = 'monthly' "
             "AND edition = '2026-07' ORDER BY version")
    rows = [r for r in outbox() if r["edition_id"] and r["edition_id"] == (pubs[-1]["edition_id"] if pubs else None)]
    classes = q("SELECT classification, handling FROM source_changes WHERE batch_id = "
                "(SELECT batch_id FROM source_changes ORDER BY detected_at DESC LIMIT 1)")
    record("J8", "A correction to published figures becomes a revised edition", "local integration (corrected copy of the live CBA file)",
           done["status"] == "succeeded" and len(pubs) == 2 and pubs[1]["cause"] == "revision"
           and pubs[1]["supersedes"] == pubs[0]["edition_id"] and [r["purpose"] for r in rows] == ["revised_edition"],
           {"cells_changed": changed, "changes": classes, "publications": pubs, "notice": rows})


def j9() -> None:
    n_pub, n_mail = q("SELECT count(*) AS n FROM publication_records")[0]["n"], len(outbox())

    def withdraw_history(conn):
        conn.execute("DELETE FROM observations WHERE dataset_id = 'cba_deposits' AND period_end = '2019-01-31'")
        reprocess_deposits(conn)

    edit_stored_dataset(withdraw_history)
    job = create_job(kind="source_check", report_type=None, params={}, trigger="schedule", schedule_slot="source_check:e2e-4")
    tick()
    done = wait_job(job, 1800)
    tick()
    classes = q("SELECT classification, periods, handling FROM source_changes WHERE batch_id = "
                "(SELECT batch_id FROM source_changes ORDER BY detected_at DESC LIMIT 1)")
    record("J9", "A historical backfill is recorded and raises no alert", "local integration (real engine)",
           done["status"] == "unchanged" and [c["classification"] for c in classes] == ["historical_backfill"]
           and q("SELECT count(*) AS n FROM publication_records")[0]["n"] == n_pub and len(outbox()) == n_mail,
           {"changes": classes, "status": done["status"],
            "translations": "classified and silenced by the same rule; exercised in tests/test_worker.py "
                            "(test_translations_and_backfills_are_recorded_and_announce_nothing) with a stand-in engine"})


def j10() -> None:
    """LibreOffice missing on the runner: no PDF, so the edition must not be published."""
    job = create_job(kind="report", report_type="sector", params={"period": "latest", "refresh": "latest_data",
                     "sector": "agriculture"}, trigger="manual", requested_by="owner", dispatch=False)
    path = ":".join(p for p in os.environ["PATH"].split(":") if not (Path(p) / "soffice").exists())
    run_worker(job, extra_env={"PATH": path})
    row = q("SELECT status, error_code, error_message FROM report_jobs WHERE job_id = %s", (job,))[0]
    published = q("SELECT count(*) AS n FROM publication_records WHERE report_type = 'sector'")[0]["n"]
    mails = [r for r in outbox() if r["purpose"] in ("new_edition", "manual_request") and r["edition_id"] and "sector" in r["edition_id"]]
    record("J10", "A validation failure blocks publication and email", "local integration (real engine, LibreOffice removed)",
           row["status"] == "blocked" and row["error_code"] == "validation_failed" and published == 0 and not mails,
           {"job": job, **row, "sector_publications": published})


def j11() -> None:
    evidence: dict[str, Any] = {}
    # (a) dispatch refused: a deployment whose GitHub token is wrong, against the real GitHub API
    bad = start_web(PREVIEW_PORT, {"AZMONITOR_DISPATCH_MODE": "github", "GITHUB_DISPATCH_TOKEN": "github_pat_invalid",
                                   "GITHUB_REPOSITORY": "tnhasanov/az-macro-banking", "GITHUB_WORKFLOW_REF": "main"})
    try:
        cookie = login(f"http://localhost:{PREVIEW_PORT}")
        status, body = http("POST", "/api/jobs", base=f"http://localhost:{PREVIEW_PORT}",
                            body={"report_type": "weekly", "params": {"period": "latest"}},
                            headers={"cookie": cookie, "origin": f"http://localhost:{PREVIEW_PORT}"})
    finally:
        stop(bad)
    failed = q("SELECT status, error_code, error_message FROM report_jobs WHERE job_id = %s", (body["job_id"],))[0]
    retry_status, retry = http("POST", f"/api/jobs/{body['job_id']}/retry", body={},
                               headers={"cookie": login(BASE), "origin": BASE})
    retried = wait_job(retry["job_id"], 1800)
    evidence["dispatch_refused"] = {"job": body["job_id"], "dispatch_note": body.get("dispatch"), **failed,
                                    "retry": retry["job_id"], "retry_status": retried["status"]}
    ok_a = failed["status"] == "failed" and failed["error_code"] == "dispatch_rejected" and retried["status"] == "succeeded"

    # (b) runner interrupted mid-render, then (c) the same worker wakes up after being replaced
    job = create_job(kind="report", report_type="sector", params={"period": "latest", "refresh": "latest_data",
                     "sector": "trade"}, trigger="manual", requested_by="owner", dispatch=False)
    zombie = run_worker(job, background=True)
    deadline = time.time() + 900
    while time.time() < deadline:
        stage = q("SELECT stage FROM report_jobs WHERE job_id = %s", (job,))[0]["stage"]
        if stage in ("rendering", "validating"):
            break
        time.sleep(1)
    os.kill(zombie.pid, signal.SIGSTOP)
    paused_at = q("SELECT stage, attempt, fence FROM report_jobs WHERE job_id = %s", (job,))[0]
    time.sleep(55)                                            # the 45-second lease lapses
    reaped = tick()["reaped"]
    requeued = q("SELECT status, next_attempt_at, fence FROM report_jobs WHERE job_id = %s", (job,))[0]
    wait_s = max(0.0, (requeued["next_attempt_at"] - dt.datetime.now(dt.timezone.utc)).total_seconds()) + 5
    time.sleep(wait_s)                                        # the backoff the reaper chose, not shortened
    finished = wait_job(job, 1800)
    os.kill(zombie.pid, signal.SIGCONT)
    zombie.wait(timeout=600)
    pubs = q("SELECT edition_id, job_id FROM publication_records WHERE report_type = 'sector' AND edition LIKE 'trade_%%'")
    events = q("SELECT attempt, status, message FROM job_events WHERE job_id = %s ORDER BY id", (job,))
    evidence["interrupted_and_replaced"] = {
        "job": job, "paused_at": paused_at, "reaped": reaped, "requeued": {"status": requeued["status"], "fence": requeued["fence"]},
        "backoff_waited_seconds": round(wait_s), "final": {k: finished[k] for k in ("status", "attempt", "fence", "edition_id")},
        "zombie_exit_code": zombie.returncode, "publications_of_this_edition": pubs,
        "events": [e["message"] for e in events][-8:]}
    ok_b = (paused_at["attempt"] == 1 and any(r["job_id"] == job and r["action"] == "requeued" for r in reaped)
            and finished["status"] == "succeeded" and finished["attempt"] == 2 and len(pubs) == 1)
    record("J11", "Recovery from dispatch refusal, runner interruption and stale ownership",
           "local integration; the dispatch refusal is a real call to the GitHub API with an invalid token",
           ok_a and ok_b, evidence)


def login(base: str) -> str:
    req = urllib.request.Request(base + "/api/auth/login", data=json.dumps({"passphrase": PASSPHRASE}).encode(),
                                 method="POST", headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        cookie = r.headers.get("set-cookie", "").split(";")[0]
    return cookie


def j12() -> None:
    target = q("SELECT delivery_id FROM email_outbox WHERE status = 'accepted' ORDER BY created_at LIMIT 1")[0]["delivery_id"]
    q("UPDATE email_outbox SET status = 'failed', failed_at = now(), last_error = 'simulated: provider returned 422' "
      "WHERE delivery_id = %s", (target,))
    # The capture provider files a message under its idempotency key, as Resend treats a repeated
    # key as the same message; a retry therefore rewrites that one file rather than adding another.
    capture_file = next((E2E / "mail").glob(f"cap_{target}.json"), None)
    before_mtime = capture_file.stat().st_mtime if capture_file else 0
    time.sleep(1)
    r = browser("retryEmail")
    row = q("SELECT status, attempts, idempotency_key FROM email_outbox WHERE delivery_id = %s", (target,))[0]
    resent = capture_file is not None and capture_file.stat().st_mtime > before_mtime
    record("J12", "A failed email is retried on its own from Settings", "local integration (browser + capture provider)",
           row["status"] == "accepted" and row["attempts"] >= 2 and row["idempotency_key"] == target and resent,
           {"delivery": target, "after_retry": row, "ui": r,
            "note": "provider-level failures, backoff and uncertain sends are covered in web/tests/email-db.test.ts"})


def j14() -> None:
    shutil.rmtree(E2E / "data", ignore_errors=True)
    shutil.rmtree(E2E / "outputs", ignore_errors=True)
    job = create_job(kind="report", report_type="monthly", params={"period": "latest", "refresh": "latest_data"},
                     trigger="manual", requested_by="owner", dispatch=False, nonce="fresh-runner")
    run_worker(job)
    row = q("SELECT status, edition_id FROM report_jobs WHERE job_id = %s", (job,))[0]
    events = [e["stage"] for e in q("SELECT stage FROM job_events WHERE job_id = %s ORDER BY id", (job,))]
    latest = q("SELECT edition_id FROM publication_records WHERE report_type = 'monthly' AND edition = '2026-07' "
               "ORDER BY version DESC LIMIT 1")[0]["edition_id"]
    record("J14", "A fresh runner restores the dataset and recognises what is already published",
           "local integration (empty data directory, local object store)",
           "restoring" in events and row["status"] == "reused" and row["edition_id"] == latest,
           {"job": job, "status": row["status"], "edition_id": row["edition_id"], "stages": events})


def j15() -> None:
    a = create_job(kind="report", report_type="sector", params={"period": "latest", "refresh": "latest_data",
                   "sector": "construction"}, trigger="manual", requested_by="owner", dispatch=False)
    b = create_job(kind="report", report_type="sector", params={"period": "latest", "refresh": "latest_data",
                   "sector": "transport"}, trigger="manual", requested_by="owner", dispatch=False)
    pa, pb = run_worker(a, background=True), run_worker(b, background=True)
    pa.wait(timeout=3600)
    pb.wait(timeout=3600)
    rows = {j: q("SELECT status, edition_id FROM report_jobs WHERE job_id = %s", (j,))[0] for j in (a, b)}
    spans = []
    for j in (a, b):
        ev = q("SELECT at, stage, status FROM job_events WHERE job_id = %s ORDER BY id", (j,))
        start = next(e["at"] for e in ev if e["stage"] == "restoring")
        end = next(e["at"] for e in ev if e["status"] in ("succeeded", "reused", "unchanged", "blocked", "failed"))
        spans.append((start, end))
    spans.sort()
    record("J15", "Two jobs at once take turns on the dataset and both finish", "local integration (two workers, one lease)",
           all(r["status"] == "succeeded" for r in rows.values()) and spans[0][1] <= spans[1][0],
           {"jobs": rows, "dataset_spans": [[s.isoformat(), e.isoformat()] for s, e in spans]})


def j16() -> None:
    preview = start_web(PREVIEW_PORT, {"VERCEL_ENV": "preview", "AZMONITOR_ENVIRONMENT": "preview",
                                       "AZMONITOR_EMAIL_PROVIDER": "resend", "RESEND_API_KEY": "re_not_a_real_key",
                                       "AZMONITOR_EMAIL_FROM": "Monitor <reports@example.az>",
                                       "AZMONITOR_DISPATCH_MODE": ""})
    try:
        r = browser("previewTestEmail", f"http://localhost:{PREVIEW_PORT}")
    finally:
        stop(preview)
    row = q("SELECT status, status_reason, provider FROM email_outbox WHERE environment = 'preview' "
            "ORDER BY created_at DESC LIMIT 1")
    record("J16", "A preview deployment cannot send email", "local integration (preview configuration with a Resend key present)",
           bool(row) and row[0]["status"] == "suppressed" and row[0]["provider"] is None,
           {"ui": r, "ledger": row,
            "announcements": "publication in a non-production environment records announcements as suppressed "
                             "(tests/test_worker.py::test_preview_deployments_record_announcements_but_never_queue_them)"})


ORDER = [("J1-J4", j1_to_j4), ("J13", j13), ("J5-J7", j5_to_j7), ("J8", j8), ("J9", j9), ("J10", j10),
         ("J11", j11), ("J12", j12), ("J14", j14), ("J15", j15), ("J16", j16)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the web server running at the end")
    ap.add_argument("--resume", action="store_true",
                    help="keep the state of the previous run (database, store, mail) instead of resetting it")
    ap.add_argument("--only", nargs="*", help="run only these journeys, e.g. --only J8")
    args = ap.parse_args()
    started = time.time()
    previous = {}
    if args.resume:
        previous = json.loads((E2E / "report.json").read_text()).get("journeys", {}) if (E2E / "report.json").exists() else {}
    else:
        prepare()
    REPORT.update(previous)
    web = start_web(PORT)
    try:
        for name, fn in ORDER:
            if args.only and name not in args.only:
                continue
            try:
                fn()
            except Exception as exc:  # a broken journey is a failed journey, and the run continues
                import traceback

                record(name, "journey raised an exception", "-", False,
                       {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-3000:]})
    finally:
        if not args.keep:
            stop(web)
    summary = {"ran_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "minutes": round((time.time() - started) / 60, 1), "database": DB_URL.rsplit("@", 1)[-1],
               "journeys": REPORT, "resumed_for": args.only if args.resume else None}
    (E2E / "report.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: v["passed"] for k, v in REPORT.items()}, indent=1))
    return 0 if all(v["passed"] for v in REPORT.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
