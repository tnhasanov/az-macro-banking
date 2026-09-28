"""Scheduler-neutral entry point: check sources, then generate the reports that are due.

Idempotent and locked. Steps:
  1. acquire job lock (stale locks are broken after settings.schedule.lock_stale_minutes)
  2. refresh all sources (discover -> download changed -> parse -> store vintages)
  3. run data-quality checks
  4. monthly policy: a new verified banking anchor month triggers a monthly edition once the configured
     companion tables have arrived or the grace period has passed (then a labelled partial edition)
  5. weekly policy: on the configured weekday, a digest is generated only if new observations arrived
     since the last successful digest; otherwise a no-update status is saved
  6. write state (data/state/run_due_state.json) and a structured summary
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from .. import config
from ..calc.validate import validate_all
from ..pipeline import Pipeline
from ..storage.backup import backup_database, verify_dataset
from ..storage.db import utcnow
from ..util.log import get_logger, setup_logging
from ..util.periods import now_baku

log = get_logger("run_due")


class JobLock:
    """A lock a second run cannot take, held across the whole restore-process-save cycle.

    The lock file is created with O_EXCL, so two runs starting at the same moment cannot both
    believe they hold it. A lock left behind by a crash is broken only when its process is gone or
    it is older than the configured staleness, and the reason is logged.
    """

    def __init__(self, path: Path, stale_minutes: int):
        self.path = path
        self.stale = dt.timedelta(minutes=stale_minutes)
        self.acquired = False

    def _holder_alive(self, pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError) as exc:
            return isinstance(exc, PermissionError)
        except OSError:
            return False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w") as fh:
                    json.dump({"pid": os.getpid(), "started_at": utcnow(), "host": os.uname().nodename}, fh)
                self.acquired = True
                return self
            except FileExistsError:
                info: dict[str, Any] = {}
                try:
                    info = json.loads(self.path.read_text())
                except (OSError, ValueError):
                    info = {}
                started = info.get("started_at")
                age = None
                if started:
                    try:
                        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(started)
                    except ValueError:
                        age = None
                alive = self._holder_alive(info.get("pid"))
                if alive and (age is None or age < self.stale):
                    raise RuntimeError(f"another run holds the lock since {started} (pid {info.get('pid')})")
                if age is not None and age < self.stale and not alive:
                    log.warning("lock holder pid %s is gone; taking over the lock", info.get("pid"))
                elif age is not None:
                    log.warning("breaking a stale lock held since %s", started)
                if attempt == 0:
                    self.path.unlink(missing_ok=True)
                    continue
                raise RuntimeError("could not acquire the run lock")
        raise RuntimeError("could not acquire the run lock")

    def __exit__(self, *exc):
        if self.acquired:
            self.path.unlink(missing_ok=True)


def _load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"anchor_first_seen": {}, "last_weekly_digest": None, "last_monthly_edition": None}


def _run_shell(command: str, label: str, log_path: Path) -> dict[str, Any]:
    """Run a configured persistence command and keep its output with the run."""
    import subprocess

    started = utcnow()
    try:
        proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=3600)
    except Exception as exc:
        return {"ok": False, "label": label, "error": f"{type(exc).__name__}: {exc}", "started_at": started}
    tail = (proc.stdout or "")[-2000:] + (("\n" + proc.stderr[-2000:]) if proc.stderr else "")
    log_path.write_text(f"$ {command}\nexit={proc.returncode}\n{tail}", encoding="utf-8")
    return {"ok": proc.returncode == 0, "label": label, "exit_code": proc.returncode, "output_tail": tail[-600:],
            "started_at": started, "finished_at": utcnow()}


def _publication_briefs(db, settings: dict[str, Any], state: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    """Brief each newly released publication once.

    A publication is briefed when it is *recent*, not merely when it is newly stored: a backfill
    loads years of archive in one run, and none of that is a release event. Recency is judged on the
    publication's own release date against `schedule.brief_within_days`. A brief already produced for
    a publication is not produced again, so a re-download, a translated edition or a repeated run
    changes nothing.
    """
    import datetime as _dt

    from ..publications.factpack import PublicationFacts
    from ..util.periods import today_baku

    window = int((settings.get("schedule") or {}).get("brief_within_days", 45))
    kinds = {"monetary_policy_review": "mpr_brief", "financial_stability_report": "fsr_brief",
             "policy_decision": "decision_update"}
    today = today_baku()
    pf = PublicationFacts(db, today)
    briefed = set(state.setdefault("briefed_publications", []))
    out: dict[str, Any] = {"considered": 0, "generated": [], "skipped_old": [], "already_briefed": [], "errors": []}
    for pub in pf.publications:
        kind = kinds.get(pub["pub_type"])
        if not kind:
            continue
        out["considered"] += 1
        pub_id = pub["publication_id"]
        if pub_id in briefed or any(e["edition_period"] == pub_id.replace(":", "_") and e["status"] == "generated"
                                    for e in db.editions(kind)):
            out["already_briefed"].append(pub_id)
            continue
        published = pub.get("published_at") or pub.get("announcement_date")
        if not published:
            out["skipped_old"].append({"publication_id": pub_id, "reason": "no verified release date"})
            continue
        try:
            age = (today - _dt.date.fromisoformat(published[:10])).days
        except ValueError:
            out["skipped_old"].append({"publication_id": pub_id, "reason": f"unparsable release date {published!r}"})
            continue
        if age > window:
            out["skipped_old"].append({"publication_id": pub_id, "reason": f"released {age} days ago, outside the "
                                                                           f"{window}-day briefing window"})
            continue
        if dry_run:
            out["generated"].append({"publication_id": pub_id, "kind": kind, "status": "would_generate"})
            continue
        try:
            from ..reports import generate_brief

            res = generate_brief(kind, as_of=None, lang=settings.get("language", "en"), publication_id=pub_id, db=db)
            out["generated"].append({"publication_id": pub_id, "kind": kind, "status": res.get("status"),
                                     "path": res.get("path")})
            if res.get("status") in ("generated", "unchanged"):
                briefed.add(pub_id)
        except Exception as exc:  # a brief failure never stops the run or the monthly edition
            log.exception("brief failed for %s", pub_id)
            out["errors"].append({"publication_id": pub_id, "error": f"{type(exc).__name__}: {exc}"})
    state["briefed_publications"] = sorted(briefed)
    return out


def _persistence(settings: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(settings.get("persistence") or {})
    cfg["restore_cmd"] = os.environ.get("AZMONITOR_RESTORE_CMD") or cfg.get("restore_cmd")
    cfg["save_cmd"] = os.environ.get("AZMONITOR_SAVE_CMD") or cfg.get("save_cmd")
    return cfg


def run_due(dry_run: bool = False) -> dict[str, Any]:
    settings = config.settings()
    paths = config.paths()
    paths.ensure()
    setup_logging(paths.logs_dir)
    sched = settings.get("schedule", {})
    state_path = paths.state_dir / "run_due_state.json"
    state = _load_state(state_path)
    summary: dict[str, Any] = {"started_at": utcnow(), "baku_time": now_baku().isoformat(), "status": "ok", "steps": {}}
    try:
        with JobLock(paths.state_dir / "run_due.lock", int(sched.get("lock_stale_minutes", 120))):
            # The whole cycle runs under the lock: restoring state, processing it and saving it back
            # are one unit, so a second run cannot restore over a run that is mid-flight.
            persistence = _persistence(settings)
            if persistence.get("restore_cmd") and not dry_run:
                res = _run_shell(persistence["restore_cmd"], "restore", paths.logs_dir / "restore.log")
                summary["steps"]["restore"] = res
                if not res["ok"]:
                    summary["status"] = "failed_restore"
                    summary["error"] = "restore command failed; the run stopped without touching stored state"
                    _write_state(paths, state_path, state, summary)
                    return summary
                check = verify_dataset(paths.db_path, min_documents=int(persistence.get("min_documents", 1)))
                summary["steps"]["restore_verification"] = check
                if not check["ok"]:
                    summary["status"] = "failed_restore"
                    summary["error"] = "restored dataset failed verification: " + "; ".join(check["problems"])
                    _write_state(paths, state_path, state, summary)
                    return summary
            if not dry_run and persistence.get("backup", True):
                try:
                    summary["steps"]["backup"] = backup_database(paths.db_path, paths.data_dir / "backups",
                                                                 keep=int(persistence.get("keep_backups", 7)))
                except Exception as exc:
                    summary["steps"]["backup"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    summary["status"] = "partial"
            p = Pipeline()
            # 1. refresh
            refresh = p.refresh()
            failed = {k: v["errors"] for k, v in refresh["datasets"].items() if v["errors"]}
            changed = {k: v for k, v in refresh["datasets"].items() if v["new_obs"] or v["revisions"]}
            summary["steps"]["refresh"] = {"changed_datasets": list(changed), "failed_datasets": failed, "run_id": refresh["run_id"]}
            # 2. quality
            q = validate_all(p.db, write=True)
            summary["steps"]["validate"] = q["summary"]
            # 3. monthly policy: the fingerprint decides, not the calendar
            rcfg = config.reports_config()["monthly"]
            states = {k: dict(v) for k, v in p.db.dataset_states().items()}
            anchor_periods = []
            for dsid in rcfg["anchors"]["banking"]:
                st = states.get(dsid)
                if not st or not st["latest_period_end"] or not (st["status"] or "").startswith(("parsed", "unchanged")):
                    anchor_periods = []
                    break
                anchor_periods.append(st["latest_period_end"])
            monthly_action = "none"
            if not anchor_periods:
                monthly_action = "blocked_missing_anchor"
                summary["status"] = "partial"
            else:
                anchor = min(anchor_periods)
                state["anchor_first_seen"].setdefault(anchor, utcnow())
                companions_ready = all((states.get(c) or {}).get("latest_period_end") and
                                       (states.get(c) or {})["latest_period_end"] >= anchor for c in rcfg["companions"])
                if dry_run:
                    monthly_action = "would_check_fingerprint"
                else:
                    from ..reports import generate_monthly

                    facts_only_flag = settings.get("narrative", {}).get("provider", "none") != "api"
                    narrative_file = settings.get("narrative", {}).get("file") or None
                    res = generate_monthly(None, facts_only_flag, narrative_file, settings.get("language", "en"),
                                           force=False, db=p.db)
                    summary["steps"]["monthly"] = res
                    monthly_action = res.get("status", "unknown")
                    if res.get("status") == "generated":
                        state["last_monthly_edition"] = {"banking_period": anchor, "status": "generated",
                                                         "complete": companions_ready, "at": utcnow(),
                                                         "path": res.get("path"), "trigger": (res.get("trigger") or {})}
                    elif res.get("status") == "blocked":
                        summary["status"] = "partial"
                summary["steps"]["monthly_policy"] = {"anchor": anchor_periods, "companions_ready": companions_ready,
                                                      "action": monthly_action,
                                                      "note": "an edition is produced whenever the inputs it reports on "
                                                              "change, including a new publication or a revision, and "
                                                              "not otherwise"}
            summary["steps"].setdefault("monthly_policy", {"anchor": anchor_periods, "action": monthly_action})
            # 3b. publication briefs: released-based, deduplicated, and quiet during a backfill
            summary["steps"]["publications"] = _publication_briefs(p.db, settings, state, dry_run)
            # 4. weekly policy
            weekday = int(sched.get("weekly_digest_weekday", 1))
            weekly_action = "not_due"
            if now_baku().weekday() == weekday or dry_run:
                last_w = state.get("last_weekly_digest")
                since = (last_w or {}).get("at") or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
                new_v = p.db.vintages_since(since)
                if new_v and not dry_run:
                    from ..render.weekly import generate_weekly

                    res = generate_weekly(None, since[:10], settings.get("language", "en"), db=p.db)
                    summary["steps"]["weekly"] = res
                    weekly_action = res.get("status", "generated")
                    if res.get("status") == "generated":
                        state["last_weekly_digest"] = {"at": utcnow(), "path": res.get("path")}
                elif not new_v:
                    weekly_action = "no_update"
                    (paths.state_dir / "weekly_status.json").write_text(json.dumps({"status": "no_update", "at": utcnow(), "since": since,
                                                                                    "note": "no new observations since the last digest; previous deck remains current"}, indent=2), encoding="utf-8")
                else:
                    weekly_action = "would_generate"
            summary["steps"]["weekly_policy"] = {"action": weekly_action}
            if failed:
                summary["status"] = "partial"
            p.db.close()
            if persistence.get("save_cmd") and not dry_run:
                res = _run_shell(persistence["save_cmd"], "save", paths.logs_dir / "save.log")
                summary["steps"]["save"] = res
                if not res["ok"]:
                    summary["status"] = "failed_save"
                    summary["error"] = ("save command failed; this run's outputs exist locally but were not "
                                        "copied to persistent storage")
    except RuntimeError as exc:
        summary["status"] = "skipped_locked"
        summary["error"] = str(exc)
        return summary
    except Exception as exc:  # collection/parse/render failures never overwrite prior outputs
        log.exception("run-due failed")
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
    _write_state(paths, state_path, state, summary)
    return summary


def _write_state(paths, state_path: Path, state: dict[str, Any], summary: dict[str, Any]) -> None:
    """Record the run. A failed run updates the run log only: the last successful edition and the
    `outputs/latest` pointer are left exactly as they were."""
    summary.setdefault("finished_at", utcnow())
    state["last_run"] = summary
    if summary.get("status") in ("ok", "partial"):
        state["last_successful_run"] = {"at": summary["finished_at"], "status": summary["status"]}
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (paths.state_dir / "last_run_due.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
