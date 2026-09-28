"""The scheduled entry point: one lock, one protected cycle, three tasks.

Every task runs inside the same structure, because they all touch the same dataset:

    lock → restore → verify → back up → [the task] → save → unlock

Restore, work and save are one unit under one lock, so a second run cannot restore over a run in
flight, and a failed restore stops everything before a single report is produced from an incomplete
dataset. That part is unchanged from the original `run-due`; what is new is what happens in the
middle, and that delivery follows production inside the same protected section.

Nothing here decides whether content changed — the edition fingerprint does that, and an unchanged
input still produces no new edition. These tasks decide *when to look* and *whether the data are fit
to report on*, and then hand over.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .. import config
from ..calc.validate import validate_all
from ..pipeline import Pipeline
from ..storage.backup import backup_database, verify_dataset
from ..storage.db import utcnow
from ..util.log import get_logger, setup_logging
from . import readiness as R
from . import tasks as T
from .run_due import JobLock, _load_state, _persistence, _publication_briefs, _run_shell, _write_state

log = get_logger("runner")


def _deliver(report_type: str, path: str | None, summary: dict[str, Any], dry_run: bool,
             sector: str | None = None) -> None:
    """Deliver a produced edition, recording the outcome in the run summary.

    A delivery failure never fails the run: the report exists and is on disk, and the ledger will
    retry or ask for a person. Losing the run over a mail server having a bad minute would be the
    wrong trade.
    """
    if not path:
        return
    try:
        from ..delivery import dispatch

        res = dispatch.deliver_edition(report_type, Path(path), dry_run=dry_run, sector=sector)
        summary.setdefault("delivery", []).append(res)
        summary["delivered_count"] = summary.get("delivered_count", 0) + sum(
            1 for d in res.get("deliveries", []) if d.get("outcome") == "sent")
    except Exception as exc:
        log.exception("delivery failed for %s", report_type)
        summary.setdefault("delivery_errors", []).append(
            {"report_type": report_type, "error": f"{type(exc).__name__}: {exc}",
             "note": "the report was produced and is on disk; only its delivery failed"})


def _alert(severity: str, headline: str, detail: str, summary: dict[str, Any], dry_run: bool) -> None:
    try:
        from ..delivery import dispatch

        summary.setdefault("alerts", []).append(
            {"severity": severity, "headline": headline,
             **dispatch.send_alert(severity, headline, detail, dry_run=dry_run)})
    except Exception as exc:  # an alert that cannot be sent is still recorded where a person looks
        log.exception("alert delivery failed")
        summary.setdefault("alerts", []).append({"severity": severity, "headline": headline,
                                                 "error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------- the tasks

def _task_source_check(p: Pipeline, state: dict[str, Any], summary: dict[str, Any], dry_run: bool) -> None:
    """Refresh the sources and produce whatever the readiness rules now allow."""
    settings = config.settings()
    sched = config.schedule_config()
    today = T.now_local().date()

    refresh = p.refresh()
    failed = {k: v["errors"] for k, v in refresh["datasets"].items() if v["errors"]}
    changed = {k: v for k, v in refresh["datasets"].items() if v["new_obs"] or v["revisions"]}
    summary["steps"]["refresh"] = {"changed_datasets": sorted(changed), "failed_datasets": failed,
                                   "run_id": refresh["run_id"]}
    if failed:
        summary["status"] = "partial"

    quality = validate_all(p.db, write=True)
    summary["steps"]["validate"] = quality["summary"]

    stale = R.stale_sources(p.db, sched.get("monitoring") or {}, today)
    summary["steps"]["stale_sources"] = stale

    checks = R.evaluate_all(p.db, today, quality)
    summary["steps"]["readiness"] = {k: v.as_dict() for k, v in checks.items()}
    produced: list[dict[str, Any]] = []

    # --- monthly, and the sector reviews that follow it
    monthly = checks.get("monthly")
    if monthly and monthly.ok:
        from ..reports import generate_monthly

        narrative_file = (settings.get("narrative") or {}).get("file") or None
        facts_only = (settings.get("narrative") or {}).get("provider", "none") != "api"
        # a dry run goes as far as the fingerprint and stops, so it reports what a real run would do
        # rather than what readiness alone permits
        res = generate_monthly(None, facts_only, narrative_file, settings.get("language", "en"),
                               force=False, db=p.db, dry_run=dry_run)
        res["readiness"] = monthly.state
        summary["steps"]["monthly"] = res
        produced.append({"report_type": "monthly", "status": res.get("status"), "path": res.get("path")})
        if not dry_run:
            if res.get("status") == "generated":
                state["last_monthly_edition"] = {"at": utcnow(), "path": res.get("path"),
                                                 "partial": monthly.state == "partial"}
                _deliver("monthly", res.get("path"), summary, dry_run)
                _sector_reviews(p, sched, summary, produced, dry_run, today)
            elif res.get("status") == "blocked":
                summary["status"] = "partial"
                _alert("error", "A monthly edition was blocked",
                       json.dumps({"cause": res.get("cause"), "failed_checks": res.get("failed_checks", [])[:5]},
                                  indent=2), summary, dry_run)
        elif res.get("status") == "would_generate":
            _sector_reviews(p, sched, summary, produced, dry_run, today)
    elif monthly and monthly.state == "stale":
        _alert("warning", "The banking tables have gone quiet",
               json.dumps(monthly.as_dict(), indent=2, default=str), summary, dry_run)

    # --- publication briefs, one per release, never one per download
    briefs = _publication_briefs(p.db, settings, state, dry_run)
    summary["steps"]["publications"] = briefs
    for got in briefs.get("generated", []):
        kind, path = got.get("kind"), got.get("path")
        produced.append({"report_type": kind, "status": got.get("status"), "path": path,
                         "publication_id": got.get("publication_id")})
        if got.get("status") == "generated":
            _deliver(kind, path, summary, dry_run)
    if briefs.get("errors"):
        summary["status"] = "partial"

    summary["produced"] = produced


def _sector_reviews(p: Pipeline, sched: dict[str, Any], summary: dict[str, Any],
                    produced: list[dict[str, Any]], dry_run: bool, today: dt.date) -> None:
    """Sector reviews follow the monthly edition, on the same data, if their own inputs are fit."""
    cfg = (sched.get("releases") or {}).get("sector") or {}
    sectors = cfg.get("sectors") or []
    if not sectors:
        return
    res = R.evaluate_sector(p.db, cfg.get("readiness") or {}, today)
    summary["steps"].setdefault("sector_readiness", res.as_dict())
    if not res.ok:
        return
    for sector in sectors:
        if dry_run:
            produced.append({"report_type": "sector", "sector": sector, "status": "would_generate"})
            continue
        try:
            from ..render.sector import generate_sector

            out = generate_sector(None, sector, config.settings().get("language", "en"), db=p.db)
            produced.append({"report_type": "sector", "sector": sector, "status": out.get("status"),
                             "path": out.get("path")})
            if out.get("status") == "generated":
                _deliver("sector", out.get("path"), summary, dry_run, sector=sector)
        except Exception as exc:
            log.exception("sector review failed for %s", sector)
            summary.setdefault("errors", []).append({"sector": sector, "error": f"{type(exc).__name__}: {exc}"})


def _task_weekly(p: Pipeline, state: dict[str, Any], summary: dict[str, Any], dry_run: bool) -> None:
    """The digest for the week that has just ended."""
    sched = config.schedule_config()
    cfg = sched.get("weekly_digest") or {}
    start, end = T.previous_calendar_week()
    lo, hi = T.week_bounds_utc(start, end)
    summary["steps"]["window"] = {"start": start.isoformat(), "end": end.isoformat(),
                                  "start_utc": lo, "end_utc": hi,
                                  "note": "the previous calendar week in Asia/Baku, Monday to Sunday"}

    vintages = [v for v in p.db.vintages_since(lo) if (v["created_at"] or "") <= hi]
    summary["steps"]["evidence"] = {"vintages_in_window": len(vintages)}
    if not vintages and cfg.get("skip_when_empty"):
        summary["steps"]["weekly"] = {"status": "skipped_empty",
                                      "note": "nothing was published in the window and skip_when_empty is set"}
        summary["produced"] = []
        return
    if dry_run:
        summary["steps"]["weekly"] = {"status": "would_generate"}
        summary["produced"] = [{"report_type": "weekly", "status": "would_generate"}]
        return

    from ..render.weekly import generate_weekly

    res = generate_weekly(None, start.isoformat(), config.settings().get("language", "en"),
                          force=not cfg.get("skip_when_empty", False), db=p.db, until=end.isoformat())
    summary["steps"]["weekly"] = res
    summary["produced"] = [{"report_type": "weekly", "status": res.get("status"), "path": res.get("path")}]
    if res.get("status") == "generated":
        state["last_weekly_digest"] = {"at": utcnow(), "path": res.get("path"),
                                       "window": [start.isoformat(), end.isoformat()]}
        _deliver("weekly", res.get("path"), summary, dry_run)


def _task_monitor(p: Pipeline, state: dict[str, Any], summary: dict[str, Any], dry_run: bool) -> None:
    """Is the system healthy? Missed runs, quiet sources, deliveries waiting on a person."""
    sched = config.schedule_config()
    today = T.now_local().date()
    missed = T.missed_runs()
    stale = R.stale_sources(p.db, sched.get("monitoring") or {}, today)
    fails = T.consecutive_failures()

    try:
        from ..delivery import dispatch

        delivery = dispatch.status()
    except Exception as exc:  # pragma: no cover - the ledger is local and rarely unavailable
        delivery = {"error": f"{type(exc).__name__}: {exc}"}

    summary["steps"]["monitor"] = {"missed_runs": missed, "stale_sources": stale,
                                   "consecutive_failures": fails,
                                   "deliveries": delivery.get("counts", {}),
                                   "deliveries_needing_review": len(delivery.get("needs_review", []))}
    summary["produced"] = []

    problems: list[str] = []
    if missed:
        problems.append(f"{len(missed)} scheduled run(s) did not happen, the earliest due "
                        f"{missed[0]['due_local']}")
    if stale:
        problems.append(f"{len(stale)} source(s) have gone quiet: " +
                        ", ".join(f"{s['dataset_id']} ({s['age_days']}d)" for s in stale[:4]))
    if delivery.get("needs_review"):
        problems.append(f"{len(delivery['needs_review'])} delivery(ies) ended without a clear answer and need "
                        f"a person to resolve them")
    threshold = int((sched.get("monitoring") or {}).get("alert_after_consecutive_failures", 2))
    if fails >= threshold:
        problems.append(f"{fails} runs in a row have failed")

    if problems:
        severity = "error" if (fails >= threshold or missed) else "warning"
        _alert(severity, "; ".join(problems[:2]),
               json.dumps(summary["steps"]["monitor"], indent=2, default=str), summary, dry_run)
    else:
        summary["steps"]["monitor"]["note"] = "nothing needed attention; no alert was sent"


# ------------------------------------------------------------------- the wrapper

_TASKS = {"source-check": _task_source_check, "weekly-digest": _task_weekly, "monitor": _task_monitor}


def run_task(task: str, dry_run: bool = False) -> dict[str, Any]:
    """Run one scheduled task inside the protected cycle."""
    if task not in _TASKS:
        raise ValueError(f"unknown task {task!r}; known tasks: {', '.join(sorted(_TASKS))}")
    settings = config.settings()
    paths = config.paths()
    paths.ensure()
    setup_logging(paths.logs_dir)
    sched_settings = settings.get("schedule", {})
    state_path = paths.state_dir / "run_due_state.json"
    state = _load_state(state_path)
    summary: dict[str, Any] = {"task": task, "started_at": utcnow(),
                               "local_time": T.now_local().isoformat(timespec="seconds"),
                               "status": "ok", "dry_run": dry_run, "steps": {}}
    p: Pipeline | None = None
    try:
        with JobLock(paths.state_dir / "run_due.lock", int(sched_settings.get("lock_stale_minutes", 120))):
            persistence = _persistence(settings)
            if persistence.get("restore_cmd") and not dry_run:
                res = _run_shell(persistence["restore_cmd"], "restore", paths.logs_dir / "restore.log")
                summary["steps"]["restore"] = res
                if not res["ok"]:
                    summary.update({"status": "failed_restore",
                                    "error": "restore command failed; the run stopped without touching stored state"})
                    _finish(paths, state_path, state, summary, task)
                    return summary
                check = verify_dataset(paths.db_path, min_documents=int(persistence.get("min_documents", 1)))
                summary["steps"]["restore_verification"] = check
                if not check["ok"]:
                    summary.update({"status": "failed_restore",
                                    "error": "restored dataset failed verification: " + "; ".join(check["problems"])})
                    _finish(paths, state_path, state, summary, task)
                    return summary
            if not dry_run and persistence.get("backup", True):
                try:
                    summary["steps"]["backup"] = backup_database(paths.db_path, paths.data_dir / "backups",
                                                                 keep=int(persistence.get("keep_backups", 7)))
                except Exception as exc:
                    summary["steps"]["backup"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    summary["status"] = "partial"

            p = Pipeline()
            _TASKS[task](p, state, summary, dry_run)
            p.db.close()
            p = None

            if persistence.get("save_cmd") and not dry_run:
                res = _run_shell(persistence["save_cmd"], "save", paths.logs_dir / "save.log")
                summary["steps"]["save"] = res
                if not res["ok"]:
                    summary.update({"status": "failed_save",
                                    "error": "save command failed; this run's outputs exist locally but were not "
                                             "copied to persistent storage"})
    except RuntimeError as exc:
        summary.update({"status": "skipped_locked", "error": str(exc)})
        return summary
    except Exception as exc:
        log.exception("task %s failed", task)
        summary.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        _alert("error", f"The {task} run failed", f"{type(exc).__name__}: {exc}", summary, dry_run)
    finally:
        if p is not None:
            p.db.close()
    _finish(paths, state_path, state, summary, task)
    return summary


def _finish(paths, state_path: Path, state: dict[str, Any], summary: dict[str, Any], task: str) -> None:
    summary.setdefault("finished_at", utcnow())
    _write_state(paths, state_path, state, summary)
    (paths.state_dir / f"last_{task.replace('-', '_')}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not summary.get("dry_run"):
        T.record_run(task, summary.get("status", "unknown"), summary)
