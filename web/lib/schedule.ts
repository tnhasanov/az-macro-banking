/**
 * When a scheduled run counts as missed.
 *
 * The engine's primary trigger is GitHub Actions cron. This is the second opinion: it looks at
 * whether the run that should have happened actually happened, and asks for one only if it did
 * not. Getting the threshold wrong in either direction is a real failure — too short and every
 * slow run is dispatched twice, too long and a scheduler that has silently stopped goes unnoticed
 * for days — so the arithmetic lives here, apart from the request handling, where it can be tested
 * against the schedule it is derived from.
 */

/**
 * How long after its last successful run a task is overdue, in minutes.
 *
 * Each is the longest gap between two consecutive runs of that task, plus 90 minutes for a run
 * that is merely slow or a scheduler that is merely late. The gaps come from
 * config/schedule.yaml, which `monitor schedule check` keeps in step with the workflow.
 *
 *   source-check   09:15, 13:15, 17:15 Baku  → longest gap is overnight, 16h
 *   weekly-digest  Monday 08:30 Baku         → 7 days
 *   monitor        07:45, 18:45 Baku         → longest gap is overnight, 13h
 */
export const OVERDUE_MINUTES: Record<string, number> = {
  "source-check": 16 * 60 + 90,
  "weekly-digest": 7 * 24 * 60 + 120,
  monitor: 13 * 60 + 90,
};

export type Decision =
  | { act: "unknown-task" }
  | { act: "wait"; reason: string; holder?: string; expiresAt?: string }
  | { act: "dispatch"; reason: string };

export interface WatchdogInput {
  task: string;
  now: Date;
  /** When the task last started. Null means it has never run. */
  lastRunAt: Date | null;
  /** Leases currently in the table; an expired one does not count as held. */
  locks: { holder: string; expires_at: string; expired: boolean }[];
}

export function decide(input: WatchdogInput): Decision & { minutesSince: number | null } {
  const overdueAfter = OVERDUE_MINUTES[input.task];
  if (!overdueAfter) return { act: "unknown-task", minutesSince: null };

  // A run in progress owns the dataset. Nothing is dispatched while the lease is held — not even
  // when the last *recorded* run looks old, because the run under way has not recorded itself yet.
  const active = input.locks.find((l) => !l.expired);
  if (active) {
    return {
      act: "wait", reason: "a run holds the lease",
      holder: active.holder, expiresAt: active.expires_at, minutesSince: null,
    };
  }

  const minutesSince = input.lastRunAt
    ? (input.now.getTime() - input.lastRunAt.getTime()) / 60000
    : null;

  // Never run at all: dispatch, rather than waiting forever for a first run that nothing will
  // start. A fresh deployment with an empty history is exactly when the primary schedule has not
  // taken effect yet.
  if (minutesSince === null) {
    return { act: "dispatch", reason: "no run of this task has ever been recorded", minutesSince: null };
  }

  // A clock skew or a restored backup can put the last run in the future. Treat that as on time
  // rather than dispatching on a negative interval.
  if (minutesSince < overdueAfter) {
    return { act: "wait", reason: "the scheduled run is on time", minutesSince };
  }

  return {
    act: "dispatch",
    reason: `the last run was ${Math.round(minutesSince)} minutes ago, past the `
      + `${overdueAfter}-minute threshold for this task`,
    minutesSince,
  };
}
