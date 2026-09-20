/**
 * Which scheduled runs should have happened, and whether they did.
 *
 * The bug this replaces: the watchdog compared elapsed time since the last run against a threshold.
 * For the weekly digest — Monday 08:30, so seven days between healthy runs — any threshold wide
 * enough not to fire on a good week is also wide enough to miss a skipped Monday until the next
 * one. `a_missed_monday_digest_is_noticed_on_monday` is that case.
 *
 * The other failure mode is the opposite: dispatching a second worker on top of a healthy one. A
 * run in progress has not recorded itself yet, so "no run found" and "no run happened" are not the
 * same statement, and the lease is what tells them apart.
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  BAKU_OFFSET_HOURS, bakuWeekday, evaluate, isSuccessful, occurrences, SCHEDULE, shouldDispatch,
} from "../lib/schedule.ts";

const NO_LOCKS: { holder: string; expires_at: string; expired: boolean }[] = [];
const HELD = [{ holder: "github-actions/42.1", expires_at: "2026-09-21T12:00:00Z", expired: false }];
const LAPSED = [{ holder: "github-actions/41.1", expires_at: "2026-09-20T01:00:00Z", expired: true }];

function run(task: string, iso: string, status = "ok") {
  return { task, started_at: iso, status };
}

// 2026-09-21 is a Monday. 09:00 UTC = 13:00 Baku, so Monday's 08:30 digest is long past.
const MONDAY_MIDDAY = new Date("2026-09-21T09:00:00Z");

test("Asia/Baku really is UTC+4, all year", () => {
  // The whole conversion rests on this. Checked against the IANA data rather than assumed.
  for (const month of ["01", "04", "07", "10"]) {
    const instant = new Date(`2026-${month}-15T12:00:00Z`);
    const local = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Baku", hour: "2-digit", hour12: false,
    }).format(instant);
    assert.equal(Number.parseInt(local, 10), (12 + BAKU_OFFSET_HOURS) % 24,
      `Asia/Baku is not UTC+${BAKU_OFFSET_HOURS} in month ${month}`);
  }
});

test("a Baku weekday is the local one, not the UTC one", () => {
  // 21:00 UTC on Sunday is already Monday in Baku.
  assert.equal(bakuWeekday(new Date("2026-09-20T21:00:00Z")), 1);
  assert.equal(bakuWeekday(new Date("2026-09-20T19:00:00Z")), 7);
});

test("occurrences land on the local times the schedule names", () => {
  const found = occurrences(SCHEDULE["source-check"], new Date("2026-09-21T18:00:00Z"), 1);
  const asBaku = found.map((o) => new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Baku", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(o.due));
  for (const t of asBaku) assert.ok(["09:15", "13:15", "17:15"].includes(t), `unexpected ${t}`);
});

test("the weekly digest only ever falls on a Monday", () => {
  const found = occurrences(SCHEDULE["weekly-digest"], new Date("2026-09-25T12:00:00Z"), 14);
  assert.ok(found.length >= 2);
  for (const o of found) assert.equal(bakuWeekday(o.due), 1, `${o.due.toISOString()} is not a Monday`);
});

// --------------------------------------------------------------- the reported defect

test("a missed Monday digest is noticed on Monday, not the following week", () => {
  const verdict = evaluate({
    task: "weekly-digest",
    now: MONDAY_MIDDAY,
    // The last digest ran a week ago and worked. Elapsed time since it is well under a week.
    runs: [run("weekly-digest", "2026-09-14T04:30:00Z")],
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "missed");
  assert.ok(shouldDispatch(verdict), "this Monday's digest must be dispatched today");
});

test("a digest that ran this Monday is left alone", () => {
  const verdict = evaluate({
    task: "weekly-digest",
    now: MONDAY_MIDDAY,
    runs: [run("weekly-digest", "2026-09-21T04:31:00Z")],   // 08:31 Baku, just after due
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "satisfied");
  assert.equal(shouldDispatch(verdict), false);
});

test("a run from before the occurrence does not satisfy it", () => {
  // 04:00 UTC is 08:00 Baku — half an hour before the digest was due.
  const verdict = evaluate({
    task: "weekly-digest",
    now: MONDAY_MIDDAY,
    runs: [run("weekly-digest", "2026-09-21T04:00:00Z")],
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "missed");
});

test("the digest is not chased before its grace period is up", () => {
  // 05:00 UTC = 09:00 Baku, half an hour after due, inside the two-hour grace. The previous
  // Monday ran, so today's occurrence is the only one in question.
  const verdict = evaluate({
    task: "weekly-digest",
    now: new Date("2026-09-21T05:00:00Z"),
    runs: [run("weekly-digest", "2026-09-14T04:31:00Z")],
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "satisfied", "the newest occurrence past grace is last Monday's");
  assert.equal(shouldDispatch(verdict), false);
});

test("an older missed occurrence is still reported once it is past grace", () => {
  // Nothing has run for a fortnight. The evaluator looks back, so this does not go unnoticed.
  const verdict = evaluate({
    task: "weekly-digest",
    now: new Date("2026-09-21T05:00:00Z"),
    runs: [],
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "missed");
  assert.equal(verdict.occurrence.label, "Monday 08:30");
});

// ------------------------------------------------------------------ several a day

test("each source check is judged on its own", () => {
  // 13:00 UTC = 17:00 Baku: the 09:15 and 13:15 runs are due, 17:15 is not.
  const now = new Date("2026-09-21T13:00:00Z");
  const satisfied = evaluate({
    task: "source-check", now,
    runs: [run("source-check", "2026-09-21T09:20:00Z")],   // 13:20 Baku, just after the 13:15 run
    locks: NO_LOCKS,
  });
  assert.equal(satisfied.state, "satisfied");

  const missed = evaluate({
    task: "source-check", now,
    runs: [run("source-check", "2026-09-21T05:20:00Z")],   // only the 09:15 run happened
    locks: NO_LOCKS,
  });
  assert.equal(missed.state, "missed");
  assert.equal(missed.occurrence.label, "13:15");
});

test("the overnight gap is not mistaken for a missed run", () => {
  // 04:00 UTC = 08:00 Baku. The last occurrence was 17:15 yesterday and it ran.
  const verdict = evaluate({
    task: "source-check",
    now: new Date("2026-09-21T04:00:00Z"),
    runs: [run("source-check", "2026-09-20T13:20:00Z")],
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "satisfied");
});

// ------------------------------------------------------- delayed, failed, duplicated

test("a run in progress is late, not missing", () => {
  const verdict = evaluate({
    task: "weekly-digest", now: MONDAY_MIDDAY,
    runs: [],                       // it has not recorded itself yet
    locks: HELD,
  });
  assert.equal(verdict.state, "running");
  assert.equal(shouldDispatch(verdict), false, "dispatching here is how you get two workers");
});

test("a lapsed lease does not stop a dispatch", () => {
  const verdict = evaluate({
    task: "weekly-digest", now: MONDAY_MIDDAY, runs: [], locks: LAPSED,
  });
  assert.equal(verdict.state, "missed");
});

test("a run that failed counts as attempted, so the watchdog does not loop", () => {
  const verdict = evaluate({
    task: "weekly-digest", now: MONDAY_MIDDAY,
    runs: [run("weekly-digest", "2026-09-21T04:31:00Z", "failed")],
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "satisfied");
  assert.equal(shouldDispatch(verdict), false,
    "a failure is the monitoring task's business; re-dispatching would loop");
  assert.equal(isSuccessful("failed"), false, "but it is still not a success");
});

test("a second check after a dispatch does not dispatch again", () => {
  // The dispatched run takes the lease before it records anything.
  const first = evaluate({ task: "weekly-digest", now: MONDAY_MIDDAY, runs: [], locks: NO_LOCKS });
  assert.equal(first.state, "missed");

  const whileRunning = evaluate({
    task: "weekly-digest", now: new Date(MONDAY_MIDDAY.getTime() + 60_000),
    runs: [], locks: HELD,
  });
  assert.equal(whileRunning.state, "running");

  const afterwards = evaluate({
    task: "weekly-digest", now: new Date(MONDAY_MIDDAY.getTime() + 20 * 60_000),
    runs: [run("weekly-digest", new Date(MONDAY_MIDDAY.getTime() + 60_000).toISOString())],
    locks: NO_LOCKS,
  });
  assert.equal(afterwards.state, "satisfied");
});

test("another task's runs never satisfy this one", () => {
  const verdict = evaluate({
    task: "weekly-digest", now: MONDAY_MIDDAY,
    runs: [run("source-check", "2026-09-21T05:20:00Z"), run("monitor", "2026-09-21T05:00:00Z")],
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "missed");
});

test("an unparseable timestamp is ignored rather than treated as a run", () => {
  const verdict = evaluate({
    task: "weekly-digest", now: MONDAY_MIDDAY,
    runs: [run("weekly-digest", "not-a-date")],
    locks: NO_LOCKS,
  });
  assert.equal(verdict.state, "missed");
});

test("an unknown task is refused rather than dispatched", () => {
  const verdict = evaluate({ task: "drop-tables", now: MONDAY_MIDDAY, runs: [], locks: NO_LOCKS });
  assert.equal(verdict.state, "unknown-task");
  assert.equal(shouldDispatch(verdict), false);
});

test("the scheduled tasks are exactly the ones the workflow runs", () => {
  assert.deepEqual(Object.keys(SCHEDULE).sort(), ["monitor", "source-check", "weekly-digest"]);
});

test("every task has a grace period, and none of them swallows a whole cycle", () => {
  for (const [name, s] of Object.entries(SCHEDULE)) {
    assert.ok(s.graceMinutes > 0, `${name} has no grace period`);
    // A grace longer than the gap to the next occurrence would let a miss go unnoticed for ever.
    const gapMinutes = s.weekday ? 7 * 24 * 60 : Math.min(
      ...s.times.map((t, i) => {
        const next = s.times[(i + 1) % s.times.length];
        const mins = (x: string) => Number(x.slice(0, 2)) * 60 + Number(x.slice(3));
        return ((mins(next) - mins(t)) + 24 * 60) % (24 * 60) || 24 * 60;
      }),
    );
    assert.ok(s.graceMinutes < gapMinutes,
      `${name}: a ${s.graceMinutes}-minute grace is not shorter than its ${gapMinutes}-minute cycle`);
  }
});
