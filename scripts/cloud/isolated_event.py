"""A controlled publication through the automatic path, on the real services, kept apart from production.

    python scripts/cloud/isolated_event.py --prefix isolated/<name> [--correction] [--plan]

It runs the same source-check job the scheduler starts — a real runner, the real Neon database, the
real private Blob store, the live CBA website — against a *copy* of the production dataset with the
latest deposits month withdrawn, so the live deposits file is a genuine new release to it. With
`--correction` a second check then sees a corrected copy of that file (three cells changed), which
must become a revised edition. `--plan` checks the preconditions and writes nothing.

What keeps it apart from production:

* storage — every object it writes is under `--prefix` in the same private store; the production
  dataset is only read, and the production pointer's version is compared before and after;
* database — its jobs, changes, publications and email rows have `environment = 'test'`, which the
  production dashboard never reads; the dashboard figures are rebuilt only by production jobs, and
  its source-check notes are recorded under `:test` keys;
* email — outside production every announcement is recorded as suppressed and nothing is sent.

It prints job ids, classifications, editions and counts: never a credential, an address or a
store id, because this repository's workflow logs are public.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from azmonitor.appstate import jobs as J  # noqa: E402
from azmonitor.appstate import migrate  # noqa: E402
from azmonitor.cloud import objectstore as OS  # noqa: E402
from azmonitor.cloud import readmodel as RM  # noqa: E402

PREFIX = re.compile(r"^isolated/[a-z0-9][a-z0-9-]{0,60}$")
DATASET = "cba_deposits"


def withdraw_latest_month(data_dir: Path) -> dict:
    """Remove the newest deposits month, so the live file brings it back as a new release."""
    conn = sqlite3.connect(data_dir / "monitor.sqlite")
    try:
        periods = [r[0] for r in conn.execute(
            "SELECT DISTINCT period_end FROM observations WHERE dataset_id = ? ORDER BY period_end DESC LIMIT 2",
            (DATASET,))]
        if len(periods) < 2:
            raise SystemExit(f"the dataset holds too little {DATASET} history to withdraw a month")
        latest, previous = periods
        removed = conn.execute("DELETE FROM observations WHERE dataset_id = ? AND period_end = ?",
                               (DATASET, latest)).rowcount
        # The next check must read the file again, as it would a newly published one.
        conn.execute("UPDATE documents SET status = 'superseded' WHERE dataset_id = ? AND status = 'parsed'",
                     (DATASET,))
        conn.execute("UPDATE dataset_state SET latest_period_end = ? WHERE dataset_id = ?", (previous, DATASET))
        conn.commit()
    finally:
        conn.close()
    return {"withdrawn_period": latest, "observations_removed": removed, "now_latest": previous}


def corrected_copy(store: OS.ObjectStore, out: Path) -> dict:
    """The live deposits file as last read, with the latest month's first three figures changed."""
    import openpyxl

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "data"
        OS.restore_dataset(work, store)
        conn = sqlite3.connect(work / "monitor.sqlite")
        url, stored = conn.execute(
            "SELECT document_url, stored_path FROM documents WHERE dataset_id = ? AND status = 'parsed' "
            "ORDER BY retrieved_at DESC LIMIT 1", (DATASET,)).fetchone()
        latest = conn.execute("SELECT max(period_end) FROM observations WHERE dataset_id = ?",
                              (DATASET,)).fetchone()[0]
        conn.close()
        wb = openpyxl.load_workbook(work / stored)
        ws = wb["2.11"]
        month = latest[5:7]
        # Months are text ("07") under a row per year; the last such row is the latest year's.
        row = [r for r in ws.iter_rows(min_row=7) if str(r[0].value or "").strip() == month][-1]
        changed = []
        for col in (1, 2, 3):
            old = row[col].value
            row[col].value = round(float(old) + 250.0, 1)
            changed.append({"row": row[0].row, "column": col, "was": old, "now": row[col].value})
        wb.save(out)
    return {"url": url, "period": latest, "cells_changed": changed}


def run_check(slot: str, store: OS.ObjectStore) -> dict:
    from azmonitor.jobs.worker import Worker

    conn = RM.connect()
    conn.autocommit = True
    try:
        job_id, _ = J.create_job(conn, kind="source_check", report_type=None, params={}, trigger="schedule",
                                 environment="test", requested_by="isolated-event", schedule_slot=slot)
    finally:
        conn.close()
    run_id = os.environ.get("GITHUB_RUN_ID")
    result = Worker(connect=RM.connect, store=store, run_id=int(run_id) if run_id else None).run(job_id)
    return {"job_id": job_id, "worker": {k: result.get(k) for k in ("status", "code", "message", "attempt")}}


def describe(job_ids: list[str]) -> dict:
    conn = RM.connect()
    try:
        def rows(sql, args=()):
            with conn.cursor() as cur:
                cur.execute(sql, args)
                cols = [d.name for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]

        jobs = rows("SELECT job_id, kind, report_type, status, error_code, edition_id, parent_job_id "
                    "FROM report_jobs WHERE job_id = ANY(%s) OR parent_job_id = ANY(%s) ORDER BY requested_at",
                    (job_ids, job_ids))
        ids = [j["job_id"] for j in jobs]
        changes = rows("SELECT dataset_id, classification, handling FROM source_changes "
                       "WHERE environment = 'test' AND handled_by_job = ANY(%s)", (ids,))
        pubs = rows("SELECT edition_id, report_type, edition, version, cause, supersedes, environment "
                    "FROM publication_records WHERE job_id = ANY(%s) ORDER BY published_at", (ids,))
        mail = rows("SELECT purpose, status, status_reason FROM email_outbox WHERE edition_id = ANY(%s)",
                    ([p["edition_id"] for p in pubs],))
        return {"jobs": jobs, "changes": changes, "publications": pubs, "announcements": mail}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, help="isolated/<lower-case-name>")
    ap.add_argument("--correction", action="store_true", help="then publish a corrected copy as a revision")
    ap.add_argument("--plan", action="store_true", help="check the preconditions; write nothing")
    args = ap.parse_args()
    if not PREFIX.match(args.prefix):
        raise SystemExit("--prefix must look like isolated/<lower-case-name>")

    os.environ.pop("AZMONITOR_BLOB_PREFIX", None)
    production = OS.store_from_env()
    isolated = OS.VercelBlobStore(prefix=args.prefix) if isinstance(production, OS.VercelBlobStore) \
        else OS.LocalObjectStore(Path(os.environ["AZMONITOR_OBJECT_STORE_DIR"]) / args.prefix)
    raw, before = production.get_versioned(OS.POINTER_KEY)
    report: dict = {"prefix": args.prefix, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "production_dataset_present": bool(raw)}
    if not raw:
        report["error"] = "the production store holds no dataset to copy; seed it first"
        print(json.dumps(report, indent=2, default=str))
        return 1
    if isolated.get_versioned(OS.POINTER_KEY)[0] is not None:
        report["error"] = "that prefix already holds a dataset; choose a new one"
        print(json.dumps(report, indent=2, default=str))
        return 1
    if args.plan:
        report["plan"] = "ok: the production dataset is present and the prefix is unused; nothing was written"
        print(json.dumps(report, indent=2, default=str))
        return 0

    conn = RM.connect()
    try:
        migrate(conn, applied_by="isolated-event")
    finally:
        conn.close()

    with tempfile.TemporaryDirectory() as tmp:
        seed = Path(tmp) / "seed"
        OS.restore_dataset(seed, production)
        report["seed"] = withdraw_latest_month(seed)
        OS.save_dataset(seed, isolated, based_on=None)

    # Only the deposits source is fetched: the event under test is its release, and the check must
    # not also pick up unrelated sources. The worker refuses this setting for production jobs.
    os.environ["AZMONITOR_REFRESH_DATASETS"] = DATASET
    first = run_check(f"source_check:{args.prefix}:release", isolated)
    report["release"] = {**first, **describe([first["job_id"]])}

    if args.correction:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "corrected.xlsx"
            fix = corrected_copy(isolated, out)
            os.environ["AZMONITOR_FETCH_OVERRIDES"] = json.dumps({fix["url"]: str(out)})
            second = run_check(f"source_check:{args.prefix}:correction", isolated)
            os.environ.pop("AZMONITOR_FETCH_OVERRIDES", None)
        report["correction"] = {"cells_changed": fix["cells_changed"], "period": fix["period"],
                                **second, **describe([second["job_id"]])}

    after = production.get_versioned(OS.POINTER_KEY)[1]
    report["production_pointer_unchanged"] = after == before
    report["isolated_objects"] = len(isolated.list(""))
    print(json.dumps(report, indent=2, default=str))
    ok = report["production_pointer_unchanged"] and any(
        p["cause"] == "new_data" for p in report["release"]["publications"])
    if args.correction:
        ok = ok and any(p["cause"] == "revision" for p in report["correction"]["publications"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
