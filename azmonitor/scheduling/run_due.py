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
from ..storage.db import utcnow
from ..util.log import get_logger, setup_logging
from ..util.periods import now_baku

log = get_logger("run_due")


class JobLock:
    def __init__(self, path: Path, stale_minutes: int):
        self.path = path
        self.stale = dt.timedelta(minutes=stale_minutes)

    def __enter__(self):
        if self.path.exists():
            try:
                info = json.loads(self.path.read_text())
                started = dt.datetime.fromisoformat(info["started_at"])
                if dt.datetime.now(dt.timezone.utc) - started < self.stale:
                    raise RuntimeError(f"another run holds the lock since {info['started_at']} (pid {info.get('pid')})")
                log.warning("breaking stale lock from %s", info.get("started_at"))
            except (ValueError, KeyError):
                pass
        self.path.write_text(json.dumps({"pid": os.getpid(), "started_at": utcnow()}))
        return self

    def __exit__(self, *exc):
        if self.path.exists():
            self.path.unlink()


def _load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"anchor_first_seen": {}, "last_weekly_digest": None, "last_monthly_edition": None}


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
            p = Pipeline()
            # 1. refresh
            refresh = p.refresh()
            failed = {k: v["errors"] for k, v in refresh["datasets"].items() if v["errors"]}
            changed = {k: v for k, v in refresh["datasets"].items() if v["new_obs"] or v["revisions"]}
            summary["steps"]["refresh"] = {"changed_datasets": list(changed), "failed_datasets": failed, "run_id": refresh["run_id"]}
            # 2. quality
            q = validate_all(p.db, write=True)
            summary["steps"]["validate"] = q["summary"]
            # 3. monthly policy
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
            if anchor_periods:
                anchor = min(anchor_periods)
                first_seen = state["anchor_first_seen"].setdefault(anchor, utcnow())
                companions_ready = all((states.get(c) or {}).get("latest_period_end") and (states.get(c) or {})["latest_period_end"] >= anchor for c in rcfg["companions"])
                grace_over = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(first_seen) >= dt.timedelta(days=int(sched.get("companion_grace_days", 7)))
                last = state.get("last_monthly_edition") or {}
                already = last.get("banking_period") == anchor and last.get("status") == "generated" and (last.get("complete") or not companions_ready)
                if already and not (companions_ready and not last.get("complete")):
                    monthly_action = "already_generated"
                elif companions_ready or grace_over:
                    monthly_action = "generate" if not dry_run else "would_generate"
                    if not dry_run:
                        from ..reports import generate_monthly

                        facts_only_flag = settings.get("narrative", {}).get("provider", "none") != "api"
                        res = generate_monthly(None, facts_only_flag, None, settings.get("language", "en"), force=(companions_ready and not last.get("complete")), db=p.db)
                        summary["steps"]["monthly"] = res
                        if res.get("status") in ("generated", "unchanged"):
                            state["last_monthly_edition"] = {"banking_period": anchor, "status": "generated", "complete": companions_ready, "at": utcnow(), "path": res.get("path") or res.get("existing")}
                        else:
                            summary["status"] = "partial"
                else:
                    monthly_action = f"waiting_for_companions_until_{(dt.datetime.fromisoformat(first_seen) + dt.timedelta(days=int(sched.get('companion_grace_days', 7)))).date()}"
            else:
                monthly_action = "blocked_missing_anchor"
                summary["status"] = "partial"
            summary["steps"]["monthly_policy"] = {"anchor": anchor_periods, "action": monthly_action}
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
    except RuntimeError as exc:
        summary["status"] = "skipped_locked"
        summary["error"] = str(exc)
        return summary
    except Exception as exc:  # collection/parse/render failures never overwrite prior outputs
        log.exception("run-due failed")
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
    summary["finished_at"] = utcnow()
    state["last_run"] = summary
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (paths.state_dir / "last_run_due.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary
