"""The scheduled tasks: what a timer actually invokes, and what each one is responsible for.

Three tasks, because they answer three different questions:

* `source-check`  — has anything been published? Runs several times a day. Refreshes the sources,
                    evaluates readiness, produces whatever is due and delivers it.
* `weekly-digest` — what happened last week? Runs Monday morning over the *previous calendar week*,
                    a fixed window with nameable boundaries rather than "the last seven days".
* `monitor`       — is the system itself healthy? Missed runs, quiet sources, deliveries awaiting a
                    person. Sends an alert when something needs attention, and nothing when it does not.

They share one lock, one restore-process-save cycle and one run history, because they operate on the
same dataset and two of them running at once would interleave writes.

A note on time. Everything a person reads is Asia/Baku: the schedule, the digest window, the "due
at" in a missed-run report. Everything stored is UTC. The conversion happens at the edges, here, and
nowhere else.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import config
from ..util.log import get_logger

log = get_logger("tasks")

TASKS = ("source-check", "weekly-digest", "monitor")


def tz() -> ZoneInfo:
    return ZoneInfo(config.schedule_config().get("timezone", "Asia/Baku"))


def now_local() -> dt.datetime:
    return dt.datetime.now(tz())


def previous_calendar_week(now: dt.datetime | None = None) -> tuple[dt.date, dt.date]:
    """Monday to Sunday of the week before the one containing `now`, in the reporting timezone.

    A digest produced on Monday morning covers the week that has just ended, whole. Using "the last
    seven days" instead would give two readers of two editions different boundaries, and using
    "since the previous digest" would silently stretch the window whenever a run was missed.
    """
    now = now or now_local()
    this_monday = (now.date() - dt.timedelta(days=now.weekday()))
    start = this_monday - dt.timedelta(days=7)
    return start, start + dt.timedelta(days=6)


def week_bounds_utc(start: dt.date, end: dt.date) -> tuple[str, str]:
    """The same window as UTC timestamps, for querying what was stored."""
    zone = tz()
    lo = dt.datetime.combine(start, dt.time.min, tzinfo=zone).astimezone(dt.timezone.utc)
    hi = dt.datetime.combine(end, dt.time.max, tzinfo=zone).astimezone(dt.timezone.utc)
    return lo.isoformat(timespec="seconds"), hi.isoformat(timespec="seconds")


# ------------------------------------------------------------------ run history

def _history_path() -> Path:
    return config.paths().state_dir / "run_history.json"


def load_history() -> dict[str, Any]:
    path = _history_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("run history was unreadable and has been started again")
    return {"runs": []}


def record_run(task: str, status: str, summary: dict[str, Any]) -> None:
    """Append a run to the history, trimmed to the configured retention."""
    sched = config.schedule_config()
    keep_days = int((sched.get("monitoring") or {}).get("keep_run_history_days", 60))
    hist = load_history()
    hist["runs"].append({
        "task": task, "status": status,
        "at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "at_local": now_local().isoformat(timespec="seconds"),
        "produced": summary.get("produced") or [],
        "delivered": summary.get("delivered_count", 0),
        "error": summary.get("error"),
    })
    cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).isoformat()
    hist["runs"] = [r for r in hist["runs"] if r.get("at_utc", "") >= cut][-2000:]
    hist.setdefault("last_success", {})
    if status in ("ok", "partial"):
        hist["last_success"][task] = hist["runs"][-1]["at_utc"]
    _history_path().parent.mkdir(parents=True, exist_ok=True)
    _history_path().write_text(json.dumps(hist, indent=2), encoding="utf-8")


def _scheduled_times(task: str) -> list[dt.time]:
    sched = config.schedule_config()
    if task == "source-check":
        return [dt.time.fromisoformat(t) for t in (sched.get("source_checks") or {}).get("times", [])]
    if task == "weekly-digest":
        t = (sched.get("weekly_digest") or {}).get("time")
        return [dt.time.fromisoformat(t)] if t else []
    return []


_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


def expected_runs(task: str, since: dt.datetime, until: dt.datetime) -> list[dt.datetime]:
    """Every moment this task was supposed to run between two instants, in local time."""
    times = _scheduled_times(task)
    if not times:
        return []
    weekday = None
    if task == "weekly-digest":
        cfg = config.schedule_config().get("weekly_digest") or {}
        weekday = _WEEKDAYS.get(str(cfg.get("weekday", "monday")).lower(), 0)
    out: list[dt.datetime] = []
    day = since.date()
    while day <= until.date():
        if weekday is None or day.weekday() == weekday:
            for t in times:
                moment = dt.datetime.combine(day, t, tzinfo=tz())
                if since <= moment <= until:
                    out.append(moment)
        day += dt.timedelta(days=1)
    return sorted(out)


def missed_runs(now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Scheduled runs that never happened, or happened too late to count.

    A machine that was off all day reports every run it missed, not just the most recent: "we missed
    one" and "we have not run since Tuesday" call for different reactions.
    """
    now = now or now_local()
    sched = config.schedule_config()
    grace = dt.timedelta(minutes=int((sched.get("monitoring") or {}).get("missed_run_grace_minutes", 120)))
    hist = load_history()
    runs = [r for r in hist.get("runs", []) if r.get("at_local")]
    if not runs:
        # Nothing has ever run. A fresh install has not missed anything: the schedule describes what
        # will happen once a timer is installed, not a debt accrued before it was.
        return []
    first = min(dt.datetime.fromisoformat(r["at_local"]) for r in runs)
    out: list[dict[str, Any]] = []
    for task in ("source-check", "weekly-digest"):
        done = sorted(dt.datetime.fromisoformat(r["at_local"]) for r in runs
                      if r.get("task") == task and r.get("status") in ("ok", "partial"))
        window_start = max(now - dt.timedelta(days=7), first)
        for due in expected_runs(task, window_start, now - grace):
            if not any(due <= d <= due + grace for d in done):
                out.append({"task": task, "due_local": due.isoformat(timespec="minutes"),
                            "late_by_minutes": int((now - due).total_seconds() // 60)})
    return sorted(out, key=lambda r: r["due_local"])


def consecutive_failures(task: str | None = None) -> int:
    """How many runs in a row have failed, for deciding whether an alert should escalate."""
    runs = [r for r in load_history().get("runs", []) if task is None or r.get("task") == task]
    n = 0
    for r in reversed(runs):
        if r.get("status") in ("ok", "partial"):
            break
        n += 1
    return n


# Asia/Baku is UTC+4 all year with no daylight saving, so the conversion to a UTC cron line is a
# fixed four hours. Asserted in the tests against the real timezone database rather than assumed.
BAKU_UTC_OFFSET_HOURS = 4


def _utc_cron_times(text: str) -> set[str]:
    """The `HH:MM` a cron expression fires at, read out of a workflow file."""
    import re

    out = set()
    for m in re.finditer(r'cron:\s*"(\d+)\s+(\d+)\s', text):
        out.add(f"{int(m.group(2)):02d}:{int(m.group(1)):02d}")
    return out


def _to_utc(local: dt.time) -> str:
    hour = (local.hour - BAKU_UTC_OFFSET_HOURS) % 24
    return f"{hour:02d}:{local.minute:02d}"


def workflow_drift() -> list[str]:
    """Where config/schedule.yaml and the GitHub Actions crons disagree.

    GitHub cron is UTC only, so the workflow holds the times converted. A conversion is exactly the
    kind of thing that is right when written and wrong after the next edit, so it is checked rather
    than trusted.
    """
    root = Path(__file__).resolve().parents[2]
    workflow = root / ".github" / "workflows" / "scheduled.yml"
    if not workflow.exists():
        return []                       # no worker deployment in this checkout; nothing to compare
    text = workflow.read_text(encoding="utf-8")
    have = _utc_cron_times(text)
    want = {_to_utc(t) for t in _scheduled_times("source-check")} | \
           {_to_utc(t) for t in _scheduled_times("weekly-digest")}
    problems = []
    for t in sorted(want - have):
        problems.append(f".github/workflows/scheduled.yml has no cron at {t} UTC, which is "
                        f"{(int(t[:2]) + BAKU_UTC_OFFSET_HOURS) % 24:02d}:{t[3:]} Asia/Baku in "
                        f"config/schedule.yaml")
    # the monitor task has no entry in config/schedule.yaml's own times, so it is not compared here
    return problems


def schedule_drift() -> list[str]:
    """Where config/schedule.yaml, the systemd timers and the GitHub crons disagree.

    The schedule is written in three places because neither a systemd timer nor a GitHub workflow
    can read YAML. Three copies of one fact drift apart eventually, and the drift is invisible until
    a report stops arriving, so it is compared on demand and in CI.
    """
    root = Path(__file__).resolve().parents[2]
    units = root / "deploy" / "systemd"
    problems: list[str] = workflow_drift()
    if not units.exists():
        return problems + ["deploy/systemd is missing: the schedule cannot be checked against the timers"]
    wanted = {
        "azmonitor-source-check.timer": [t.strftime("%H:%M") for t in _scheduled_times("source-check")],
        "azmonitor-weekly.timer": [t.strftime("%H:%M") for t in _scheduled_times("weekly-digest")],
    }
    for unit, times in wanted.items():
        path = units / unit
        if not path.exists():
            problems.append(f"{unit} is missing, but config/schedule.yaml expects it to run at {', '.join(times)}")
            continue
        text = path.read_text(encoding="utf-8")
        for t in times:
            if t not in text:
                problems.append(f"{unit} does not mention {t}, which config/schedule.yaml schedules")
        for line in text.splitlines():
            if line.strip().startswith("OnCalendar="):
                stamp = line.split("=", 1)[1].strip()
                if not any(t in stamp for t in times) and times:
                    problems.append(f"{unit} has OnCalendar={stamp!r}, which is not in config/schedule.yaml "
                                    f"({', '.join(times)})")
    return problems
