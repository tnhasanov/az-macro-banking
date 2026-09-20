/**
 * Which scheduled runs should have happened, and whether they did.
 *
 * The first version of this asked "how long since this task last ran?" and compared that against a
 * threshold. That cannot detect a specific missed occurrence, and for the weekly digest it fails
 * outright: the digest runs on Monday at 08:30, so the gap between two runs is seven days, so a
 * threshold wide enough not to fire on a healthy week is also wide enough to miss a skipped Monday
 * until the following Monday. A weekly report can be a week late before anyone is told.
 *
 * So this works the other way round. It enumerates the occurrences the schedule actually calls for,
 * decides which are due by now, and asks whether each one was attempted. A digest missed on Monday
 * morning is visible on Monday morning.
 *
 * Three states, kept apart on purpose:
 *
 *   **satisfied** — a run started after the occurrence was due. Nothing to do.
 *   **running**   — the occurrence is due, no run has recorded itself yet, and the lease is held.
 *                   The job is late, not missing. Dispatching here is how you get two workers.
 *   **missed**    — due, unattempted, and nobody holds the lease.
 *
 * A run that started and *failed* counts as attempted. The watchdog's job is to notice a run that
 * never happened; a run that happened and failed is the monitoring task's business, and dispatching
 * again would turn one broken cycle into a dispatch loop.
 */

/** Asia/Baku is UTC+4 all year and has no daylight saving. Checked against the IANA data by a test. */
export const BAKU_OFFSET_HOURS = 4;

export interface Occurrence {
  /** When this run was due, as an instant. */
  due: Date;
  /** Minutes after `due` before it counts as missed rather than merely late. */
  graceMinutes: number;
  /** How it reads in the schedule, e.g. "Monday 08:30" or "13:15". */
  label: string;
}

export interface TaskSchedule {
  task: string;
  /** Local times in Asia/Baku, "HH:MM". */
  times: string[];
  /** 1 = Monday … 7 = Sunday. Absent means every day. */
  weekday?: number;
  graceMinutes: number;
}

/**
 * The schedule, as config/schedule.yaml and the workflow crons define it.
 *
 * This is the fourth copy of one fact — the YAML, the systemd timers, the GitHub crons and here —
 * because none of the other three can be read from a Vercel Function at request time. A Python test
 * (`test_watchdog_schedule.py`) compares this file against the other three and fails on drift,
 * which is the same guard `monitor schedule check` already applies to the first three.
 */
export const SCHEDULE: Record<string, TaskSchedule> = {
  "source-check": {
    task: "source-check",
    times: ["09:15", "13:15", "17:15"],
    // config/schedule.yaml: source_checks.catch_up_window_minutes
    graceMinutes: 90,
  },
  "weekly-digest": {
    task: "weekly-digest",
    times: ["08:30"],
    weekday: 1,
    // monitoring.missed_run_grace_minutes
    graceMinutes: 120,
  },
  monitor: {
    task: "monitor",
    times: ["07:45", "18:45"],
    graceMinutes: 90,
  },
};

/** A run, as the read model records it. */
export interface RunRecord {
  task: string;
  started_at: string | Date;
  status: string;
}

/** Statuses that mean the cycle did its job. Anything else is a failure to report, not to retry. */
const SUCCESSFUL = new Set(["ok", "partial", "unchanged", "skipped_locked", "success"]);

/** The instant a local Baku wall-clock time falls on, for a given UTC day. */
function occurrenceAt(dayUtc: Date, hhmm: string): Date {
  const [h, m] = hhmm.split(":").map((n) => Number.parseInt(n, 10));
  return new Date(Date.UTC(
    dayUtc.getUTCFullYear(), dayUtc.getUTCMonth(), dayUtc.getUTCDate(),
    h - BAKU_OFFSET_HOURS, m, 0, 0,
  ));
}

/** The Baku-local weekday (1 = Monday) of an instant. */
export function bakuWeekday(instant: Date): number {
  const local = new Date(instant.getTime() + BAKU_OFFSET_HOURS * 3600_000);
  const day = local.getUTCDay();          // 0 = Sunday
  return day === 0 ? 7 : day;
}

/**
 * Every occurrence of a task in the window ending at `now`, oldest first.
 *
 * `lookbackDays` has to cover the longest gap in the schedule plus its grace, or a weekly
 * occurrence could fall out of the window before it is ever evaluated.
 */
export function occurrences(schedule: TaskSchedule, now: Date, lookbackDays = 9): Occurrence[] {
  const out: Occurrence[] = [];
  // Start a day early and end a day late, so an occurrence near a UTC day boundary is not lost to
  // the four-hour shift between Baku and UTC.
  for (let d = lookbackDays; d >= -1; d -= 1) {
    const day = new Date(now.getTime() - d * 86_400_000);
    for (const hhmm of schedule.times) {
      const due = occurrenceAt(day, hhmm);
      if (due.getTime() > now.getTime()) continue;
      if (schedule.weekday && bakuWeekday(due) !== schedule.weekday) continue;
      const weekdayName = schedule.weekday ? WEEKDAYS[schedule.weekday - 1] + " " : "";
      out.push({ due, graceMinutes: schedule.graceMinutes, label: `${weekdayName}${hhmm}` });
    }
  }
  out.sort((a, b) => a.due.getTime() - b.due.getTime());
  return out;
}

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export type Verdict =
  | { state: "unknown-task" }
  | { state: "satisfied"; occurrence: Occurrence; by: RunRecord }
  | { state: "not-yet-due"; occurrence: Occurrence | null }
  | { state: "running"; occurrence: Occurrence; holder: string; expiresAt: string }
  | { state: "missed"; occurrence: Occurrence; lateMinutes: number };

export interface WatchdogInput {
  task: string;
  now: Date;
  /** Every recorded run of this task in the lookback window. */
  runs: RunRecord[];
  /** Leases currently in the table. An expired one is not a lease. */
  locks: { holder: string; expires_at: string; expired: boolean }[];
}

/**
 * The most recent occurrence that is past its grace, and what happened to it.
 *
 * Only the most recent matters for deciding whether to dispatch: an older gap cannot be filled by
 * running the task now, and reporting it is the monitoring task's job.
 */
export function evaluate(input: WatchdogInput): Verdict {
  const schedule = SCHEDULE[input.task];
  if (!schedule) return { state: "unknown-task" };

  const all = occurrences(schedule, input.now);
  if (all.length === 0) return { state: "not-yet-due", occurrence: null };

  const due = all.filter(
    (o) => input.now.getTime() >= o.due.getTime() + o.graceMinutes * 60_000,
  );
  if (due.length === 0) {
    return { state: "not-yet-due", occurrence: all[all.length - 1] };
  }

  const latest = due[due.length - 1];
  const next = all.find((o) => o.due.getTime() > latest.due.getTime()) ?? null;

  // A run counts for this occurrence if it started at or after it was due, and before the next one.
  const attempt = input.runs
    .filter((r) => r.task === input.task)
    .map((r) => ({ ...r, at: new Date(r.started_at) }))
    .filter((r) => !Number.isNaN(r.at.getTime()))
    .filter((r) => r.at.getTime() >= latest.due.getTime()
      && (!next || r.at.getTime() < next.due.getTime()))
    .sort((a, b) => b.at.getTime() - a.at.getTime())[0];

  if (attempt) return { state: "satisfied", occurrence: latest, by: attempt };

  // Nothing recorded — but a run in progress has not recorded itself yet. Holding the lease is the
  // difference between "late" and "never started", and dispatching a second worker over the first
  // is precisely what the lease exists to prevent.
  const held = input.locks.find((l) => !l.expired);
  if (held) {
    return { state: "running", occurrence: latest, holder: held.holder,
             expiresAt: held.expires_at };
  }

  const lateMinutes = Math.round((input.now.getTime() - latest.due.getTime()) / 60_000);
  return { state: "missed", occurrence: latest, lateMinutes };
}

/** Whether a verdict calls for a run to be started. */
export function shouldDispatch(verdict: Verdict): boolean {
  return verdict.state === "missed";
}

export function describe(verdict: Verdict): string {
  switch (verdict.state) {
    case "unknown-task":
      return "no schedule is defined for this task";
    case "not-yet-due":
      return verdict.occurrence
        ? `the ${verdict.occurrence.label} run is not yet past its grace period`
        : "no occurrence of this task has come due yet";
    case "satisfied":
      return `the ${verdict.occurrence.label} run was attempted at `
        + `${new Date(verdict.by.started_at).toISOString()} (${verdict.by.status})`;
    case "running":
      return `the ${verdict.occurrence.label} run is under way: ${verdict.holder} holds the lease `
        + `until ${verdict.expiresAt}`;
    case "missed":
      return `the ${verdict.occurrence.label} run was due ${verdict.lateMinutes} minutes ago and `
        + "nothing has run it";
  }
}

/** Statuses the read model records that mean the cycle worked. Exported for the tests. */
export function isSuccessful(status: string): boolean {
  return SUCCESSFUL.has(status);
}
