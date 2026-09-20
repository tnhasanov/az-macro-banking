/**
 * The watchdog's decision: is the run that should have happened missing?
 *
 * Two ways to get this wrong, and both are silent. Dispatch too eagerly and every slow run is run
 * twice — the lease stops the damage, but the history fills with skipped runs and a real overlap
 * stops being visible. Dispatch too reluctantly and a scheduler that has quietly stopped (GitHub
 * disables cron in a repository with no activity for sixty days) goes unnoticed until someone
 * notices the reports stopped.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { decide, OVERDUE_MINUTES } from "../lib/schedule.ts";

const NOW = new Date("2026-09-20T12:00:00Z");
const NO_LOCKS: { holder: string; expires_at: string; expired: boolean }[] = [];

function minutesAgo(n: number): Date {
  return new Date(NOW.getTime() - n * 60_000);
}

test("a run inside its window is left alone", () => {
  for (const [task, threshold] of Object.entries(OVERDUE_MINUTES)) {
    const d = decide({ task, now: NOW, lastRunAt: minutesAgo(threshold - 1), locks: NO_LOCKS });
    assert.equal(d.act, "wait", task);
    assert.equal(d.reason, "the scheduled run is on time");
  }
});

test("a run past its window is dispatched", () => {
  for (const [task, threshold] of Object.entries(OVERDUE_MINUTES)) {
    const d = decide({ task, now: NOW, lastRunAt: minutesAgo(threshold + 1), locks: NO_LOCKS });
    assert.equal(d.act, "dispatch", task);
  }
});

test("the thresholds clear the real gaps in the schedule", () => {
  // source-check runs 09:15, 13:15, 17:15 Baku: the overnight gap is 16 hours.
  assert.ok(OVERDUE_MINUTES["source-check"] > 16 * 60, "must survive the overnight gap");
  // ...but must not swallow a whole extra day, or a stopped scheduler goes unnoticed.
  assert.ok(OVERDUE_MINUTES["source-check"] < 24 * 60, "must notice within a day");

  // monitor runs 07:45 and 18:45 Baku: the overnight gap is 13 hours.
  assert.ok(OVERDUE_MINUTES.monitor > 13 * 60);
  assert.ok(OVERDUE_MINUTES.monitor < 24 * 60);

  // weekly-digest runs once a week; anything under a week would fire every single day.
  assert.ok(OVERDUE_MINUTES["weekly-digest"] > 7 * 24 * 60);
  assert.ok(OVERDUE_MINUTES["weekly-digest"] < 9 * 24 * 60);
});

test("a held lease stops a dispatch even when the recorded run looks ancient", () => {
  // This is the case that matters: a long run in progress has not recorded itself yet, so its
  // task's last *recorded* run is the previous one and looks overdue.
  const d = decide({
    task: "source-check",
    now: NOW,
    lastRunAt: minutesAgo(60 * 24 * 7),
    locks: [{ holder: "github-actions/42.1", expires_at: "2026-09-20T12:45:00Z", expired: false }],
  });
  assert.equal(d.act, "wait");
  assert.equal(d.reason, "a run holds the lease");
  assert.equal(d.holder, "github-actions/42.1");
});

test("a lapsed lease does not stop a dispatch", () => {
  // A crashed run leaves its lease behind. If an expired lease blocked the watchdog, one crash
  // would stop the schedule permanently.
  const d = decide({
    task: "source-check",
    now: NOW,
    lastRunAt: minutesAgo(60 * 24),
    locks: [{ holder: "github-actions/41.1", expires_at: "2026-09-19T10:00:00Z", expired: true }],
  });
  assert.equal(d.act, "dispatch");
});

test("a task that has never run is dispatched rather than waited on forever", () => {
  const d = decide({ task: "monitor", now: NOW, lastRunAt: null, locks: NO_LOCKS });
  assert.equal(d.act, "dispatch");
  assert.match(d.reason, /has ever been recorded/);
  assert.equal(d.minutesSince, null);
});

test("a last run in the future is treated as on time, not as a negative interval", () => {
  // Clock skew between the runner and the database, or a restored backup, can do this.
  const d = decide({
    task: "monitor", now: NOW, lastRunAt: new Date("2026-09-21T00:00:00Z"), locks: NO_LOCKS,
  });
  assert.equal(d.act, "wait");
  assert.ok((d.minutesSince ?? 0) < 0);
});

test("repeated firings of the same cron produce one dispatch, not several", () => {
  // Cron delivery is at-least-once everywhere. The first call dispatches; once the run takes its
  // lease, every later call declines — which is what the endpoint sees on a retry.
  const first = decide({
    task: "monitor", now: NOW, lastRunAt: minutesAgo(60 * 20), locks: NO_LOCKS,
  });
  assert.equal(first.act, "dispatch");

  const whileRunning = decide({
    task: "monitor", now: new Date(NOW.getTime() + 30_000), lastRunAt: minutesAgo(60 * 20),
    locks: [{ holder: "github-actions/99.1", expires_at: "2026-09-20T12:55:00Z", expired: false }],
  });
  assert.equal(whileRunning.act, "wait");

  const afterItRecorded = decide({
    task: "monitor", now: new Date(NOW.getTime() + 15 * 60_000), lastRunAt: NOW, locks: NO_LOCKS,
  });
  assert.equal(afterItRecorded.act, "wait");
});

test("an unknown task is refused rather than dispatched", () => {
  const d = decide({ task: "drop-tables", now: NOW, lastRunAt: null, locks: NO_LOCKS });
  assert.equal(d.act, "unknown-task");
});

test("the tasks with thresholds are exactly the tasks the workflow runs", () => {
  assert.deepEqual(
    Object.keys(OVERDUE_MINUTES).sort(),
    ["monitor", "source-check", "weekly-digest"],
  );
});
