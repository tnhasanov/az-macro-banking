"""The schedule, in its fourth place.

`config/schedule.yaml` is the definition. The systemd timers and the GitHub crons repeat it because
neither can read YAML, and `monitor schedule check` compares those three. The dashboard's watchdog
is now a fourth copy, in `web/lib/schedule.ts`, because a Vercel Function cannot read the YAML
either — the repository is not on its filesystem at request time.

Four copies of one fact drift apart, and the drift is invisible until a report quietly stops
arriving. So it is compared here, in CI, the same way the other three are.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
TS = ROOT / "web" / "lib" / "schedule.ts"
WORKFLOW = ROOT / ".github" / "workflows" / "scheduled.yml"
CONFIG = ROOT / "config" / "schedule.yaml"

BAKU_OFFSET_HOURS = 4


@pytest.fixture(scope="module")
def ts_schedule() -> dict[str, dict]:
    """Parse the times out of the TypeScript, without a JavaScript runtime.

    A regex over source is usually a poor idea; here it is the point. The test must read what the
    deployed file actually says, not what an import of it would evaluate to.
    """
    text = TS.read_text(encoding="utf-8")
    body = re.search(r"export const SCHEDULE[^=]*=\s*\{(.*?)\n\};", text, re.S)
    assert body, "SCHEDULE is no longer a literal object in web/lib/schedule.ts"

    out: dict[str, dict] = {}
    for entry in re.finditer(
        r'"?([a-z-]+)"?:\s*\{\s*task:\s*"([a-z-]+)",\s*times:\s*\[([^\]]*)\],'
        r'(?:\s*weekday:\s*(\d+),)?\s*(?://[^\n]*\n\s*)*graceMinutes:\s*(\d+)',
        body.group(1),
    ):
        key, task, times, weekday, grace = entry.groups()
        assert key == task, f"{key} is filed under a different task name"
        out[task] = {
            "times": sorted(re.findall(r'"(\d\d:\d\d)"', times)),
            "weekday": int(weekday) if weekday else None,
            "grace": int(grace),
        }
    assert out, "no task schedules could be read out of web/lib/schedule.ts"
    return out


@pytest.fixture(scope="module")
def crons() -> dict[str, list[str]]:
    """The workflow's cron lines, grouped by the task each one runs."""
    text = WORKFLOW.read_text(encoding="utf-8")
    schedules = re.findall(r'- cron: "([^"]+)"\s*#\s*([a-z ]+?)\s+-', text)
    case = re.search(r'case "\$\{\{ github\.event\.schedule \}\}" in(.*?)esac', text, re.S)
    assert case, "the workflow no longer maps cron lines to tasks"

    out: dict[str, list[str]] = {}
    for line in case.group(1).splitlines():
        if ")" not in line or "TASK=" not in line:
            continue
        patterns, _, action = line.partition(")")
        task = action.split("TASK=")[1].split()[0].strip(';" ')
        for pattern in re.findall(r'"([^"]+)"', patterns):
            if pattern:
                out.setdefault(task, []).append(pattern)
    assert schedules or out
    return out


def _cron_to_baku(cron: str) -> str:
    """A UTC cron line as an Asia/Baku wall-clock time."""
    minute, hour = cron.split()[0], cron.split()[1]
    return f"{(int(hour) + BAKU_OFFSET_HOURS) % 24:02d}:{int(minute):02d}"


def test_the_dashboard_and_the_workflow_run_the_same_tasks(ts_schedule, crons):
    assert set(ts_schedule) == set(crons), (
        "the watchdog and the workflow disagree about which tasks exist: "
        f"watchdog={sorted(ts_schedule)} workflow={sorted(crons)}")


@pytest.mark.parametrize("task", ["source-check", "weekly-digest", "monitor"])
def test_the_dashboard_knows_the_times_the_workflow_actually_runs(ts_schedule, crons, task):
    from_workflow = sorted({_cron_to_baku(c) for c in crons[task]})
    assert ts_schedule[task]["times"] == from_workflow, (
        f"{task}: the watchdog expects {ts_schedule[task]['times']} Asia/Baku, but the workflow "
        f"runs at {from_workflow}")


def test_the_source_check_times_match_the_configuration(ts_schedule):
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert ts_schedule["source-check"]["times"] == sorted(cfg["source_checks"]["times"])


def test_the_weekly_digest_matches_the_configuration(ts_schedule):
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    weekly = cfg["weekly_digest"]
    assert ts_schedule["weekly-digest"]["times"] == [weekly["time"]]
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    assert ts_schedule["weekly-digest"]["weekday"] == weekdays.index(weekly["weekday"]) + 1


def test_the_monitor_times_match_the_configuration(ts_schedule):
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert ts_schedule["monitor"]["times"] == sorted(cfg["monitoring"]["times"])


def test_the_grace_periods_come_from_the_configuration(ts_schedule):
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert ts_schedule["source-check"]["grace"] == cfg["source_checks"]["catch_up_window_minutes"]
    assert ts_schedule["weekly-digest"]["grace"] == cfg["monitoring"]["missed_run_grace_minutes"]
    assert ts_schedule["monitor"]["grace"] == cfg["monitoring"]["grace_minutes"]


def test_only_the_weekly_digest_is_pinned_to_a_weekday(ts_schedule):
    assert ts_schedule["weekly-digest"]["weekday"] == 1
    assert ts_schedule["source-check"]["weekday"] is None
    assert ts_schedule["monitor"]["weekday"] is None


def test_the_watchdog_crons_run_after_the_engine_crons():
    """A watchdog that checks before the run it is watching would report every run as missed."""
    vercel = yaml.safe_load((ROOT / "web" / "vercel.json").read_text(encoding="utf-8"))
    engine = WORKFLOW.read_text(encoding="utf-8")
    engine_hours = sorted({int(c.split()[1]) * 60 + int(c.split()[0])
                           for c in re.findall(r'- cron: "([^"]+)"', engine)})
    for job in vercel["crons"]:
        minute, hour = job["schedule"].split()[0], job["schedule"].split()[1]
        if "*" in (minute, hour):
            continue
        at = int(hour) * 60 + int(minute)
        earlier = [e for e in engine_hours if e <= at]
        assert earlier, f"{job['path']} at {job['schedule']} runs before any engine cron that day"


def test_a_missed_run_grace_is_shorter_than_the_cycle_it_guards(ts_schedule):
    """A grace longer than the gap between runs would hide a miss for ever."""
    for task, spec in ts_schedule.items():
        if spec["weekday"]:
            cycle = 7 * 24 * 60
        elif len(spec["times"]) == 1:
            cycle = 24 * 60
        else:
            minutes = sorted(int(t[:2]) * 60 + int(t[3:]) for t in spec["times"])
            gaps = [b - a for a, b in zip(minutes, minutes[1:])]
            gaps.append(24 * 60 - (minutes[-1] - minutes[0]))
            cycle = min(gaps)
        assert spec["grace"] < cycle, (
            f"{task}: a {spec['grace']}-minute grace does not fit inside a {cycle}-minute cycle")
