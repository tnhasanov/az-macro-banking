/**
 * The cron endpoint's own behaviour, as opposed to the decision it delegates.
 *
 * Two properties are tested here rather than in `schedule.test.ts` because they live in the route:
 * that a database failure stops the request instead of becoming an empty array, and that dispatch
 * stays off unless it has been turned on deliberately.
 *
 * The first is the one that matters. `locks()` and the run history used to be wrapped in
 * `.catch(() => [])`, so a Neon outage produced "no lease is held" — and a watchdog that believes
 * nothing is running starts a second worker on top of a healthy one.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROUTE = readFileSync(new URL("../app/api/cron/[task]/route.ts", import.meta.url), "utf8");
const DB = readFileSync(new URL("../lib/db.ts", import.meta.url), "utf8");

test("the route does not swallow a failed database read into an empty result", () => {
  // The specific anti-pattern, spelled out so it cannot come back by copy-paste.
  assert.ok(!/catch\(\s*\(\)\s*=>\s*\[\]\s*\)/.test(ROUTE),
    "a failed query must not become an empty array in the cron route");
  assert.ok(ROUTE.includes("status: 503"),
    "a database failure must end the request, not continue with partial knowledge");
});

test("the reads the decision depends on are awaited together and guarded", () => {
  const block = ROUTE.slice(ROUTE.indexOf("let runs, held"), ROUTE.indexOf("const verdict"));
  assert.ok(block.includes("try {") && block.includes("catch"),
    "both reads must be inside one guard: either we know the state or we stop");
  assert.ok(block.includes("recentRuns") && block.includes("locks()"));
  assert.ok(block.includes("dispatched: false"),
    "the failure response must say plainly that nothing was dispatched");
});

/** The body of a top-level function, from its signature to the closing brace in column one. */
function functionBody(source: string, signature: string): string {
  const start = source.indexOf(signature);
  assert.ok(start > 0, `${signature} is missing`);
  const end = source.indexOf("\n}", start);
  assert.ok(end > start, `${signature} is not closed as expected`);
  return source.slice(start, end);
}

test("neither query helper hides its own failure", () => {
  for (const fn of ["export async function recentRuns", "export async function locks"]) {
    const body = functionBody(DB, fn);
    assert.ok(!body.includes("catch"),
      `${fn} must let an error propagate; a caller deciding whether to start a worker cannot be `
      + "handed an empty array when the truth is that the database was unreachable");
  }
});

test("dispatch is off unless it has been turned on for this deployment", () => {
  assert.ok(ROUTE.includes('process.env.AZMONITOR_CRON_ENABLED !== "true"'),
    "dispatch must be opt-in, so a preview deployment runs nothing");
  // And the check sits before the dispatch call, not after it.
  assert.ok(ROUTE.indexOf("AZMONITOR_CRON_ENABLED") < ROUTE.indexOf("await dispatchWorkflow"));
});

test("only a missed occurrence dispatches", () => {
  assert.ok(ROUTE.includes("shouldDispatch(verdict)"),
    "the route must use the schedule's verdict rather than deciding for itself");
});

test("the scheduler is authenticated before anything else happens", () => {
  assert.ok(ROUTE.indexOf("authorizeScheduler") < ROUTE.indexOf("const { task }"),
    "authorisation must precede reading the task, let alone touching the database");
});

test("a dispatch failure never echoes GitHub's response body", () => {
  const fn = ROUTE.slice(ROUTE.indexOf("async function dispatchWorkflow"));
  assert.ok(!fn.includes("response.text()") && !fn.includes("response.json()"),
    "the body can carry a token or a repository path; only the status is safe to report");
  assert.ok(fn.includes("response.status"));
});
