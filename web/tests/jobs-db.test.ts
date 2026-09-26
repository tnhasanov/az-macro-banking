/**
 * Jobs, the dispatch outbox and the scheduler tick, against a real Postgres.
 *
 * Local integration: GitHub is replaced by a dispatcher object that records what it was asked to
 * start. Everything else — the equivalence rule, the partial unique indexes, SKIP LOCKED, the
 * reaper — is the database's, exactly as in production.
 */
import test, { after, before } from "node:test";
import assert from "node:assert/strict";
import { freshDatabase, skip } from "./helpers/db.ts";

let drop: () => Promise<void> = async () => undefined;
let A: typeof import("../lib/appstate.ts");
let D: typeof import("../lib/dispatch.ts");
let S: typeof import("../lib/scheduler.ts");
let sql: ReturnType<typeof import("../lib/db.ts")["sql"]>;

before(async () => {
  if (skip) return;
  ({ drop } = await freshDatabase("webjobs"));
  process.env.AZMONITOR_ENVIRONMENT = "production";
  A = await import("../lib/appstate.ts");
  D = await import("../lib/dispatch.ts");
  S = await import("../lib/scheduler.ts");
  sql = (await import("../lib/db.ts")).sql();
  await A.ensureSchema();
});
after(async () => { if (!skip) await drop(); });

const monthly = { period: "latest", refresh: "latest_data" };

function recorder(answer: (jobId: string) => import("../lib/dispatch.ts").DispatchResult = () => ({ kind: "accepted", runId: 42, runUrl: "https://github.com/o/r/actions/runs/42" })) {
  const calls: string[] = [];
  return { calls, d: { name: "fake", async dispatch(jobId: string) { calls.push(jobId); return answer(jobId); } } };
}

test("ten simultaneous identical requests make one job", { skip }, async () => {
  const results = await Promise.all(Array.from({ length: 10 }, () => A.createJob({
    kind: "report", reportType: "monthly", params: monthly, trigger: "manual", environment: "production",
    requestedBy: "owner" })));
  const ids = new Set(results.map((r) => r.jobId));
  assert.equal(ids.size, 1);
  assert.equal(results.filter((r) => r.created).length, 1);
  const [{ n }] = await sql<{ n: number }[]>`SELECT count(*)::int AS n FROM job_dispatches WHERE job_id = ${[...ids][0]}`;
  assert.equal(n, 1);
});

test("asking to be emailed about a job someone else started attaches to it", { skip }, async () => {
  const a = await A.createJob({ kind: "report", reportType: "sector", params: { ...monthly, sector: "trade" },
    trigger: "manual", environment: "production", requestedBy: "scheduler" });
  const b = await A.createJob({ kind: "report", reportType: "sector", params: { ...monthly, sector: "trade" },
    trigger: "manual", environment: "production", requestedBy: "owner", requesterEmail: "owner@example.az",
    notifyRequester: true });
  assert.equal(b.jobId, a.jobId);
  const job = await A.getJob(a.jobId);
  assert.equal(job!.notify_requester, true);
  assert.equal(job!.requester_email, "owner@example.az");
});

test("an accepted dispatch records the run, and the job is dispatched, not running", { skip }, async () => {
  const { jobId } = await A.createJob({ kind: "report", reportType: "weekly", params: monthly, trigger: "manual",
    environment: "production", requestedBy: "owner" });
  const r = recorder();
  const out = await D.drainDispatches("production", { jobId, with: r.d });
  assert.deepEqual(r.calls, [jobId]);
  assert.equal(out.results[0].outcome, "accepted");
  const job = await A.getJob(jobId);
  assert.equal(job!.status, "dispatched");
  assert.equal(job!.run_url, "https://github.com/o/r/actions/runs/42");
  // draining again sends nothing: the row is no longer pending
  const again = await D.drainDispatches("production", { jobId, with: r.d });
  assert.equal(again.sent, 0);
});

test("GitHub being unavailable is retried with backoff; a refusal fails the job with the reason", { skip }, async () => {
  const { jobId } = await A.createJob({ kind: "report", reportType: "decision_update",
    params: { publication_id: "latest", refresh: "latest_data" }, trigger: "manual", environment: "production",
    requestedBy: "owner" });
  const flaky = recorder(() => ({ kind: "retry", message: "GitHub answered HTTP 502" }));
  await D.drainDispatches("production", { jobId, with: flaky.d });
  let [row] = await sql<{ status: string; next_try_at: Date }[]>`SELECT status, next_try_at FROM job_dispatches WHERE job_id = ${jobId}`;
  assert.equal(row.status, "pending");
  assert.ok(row.next_try_at.getTime() > Date.now() + 20_000);
  assert.equal((await A.getJob(jobId))!.status, "queued");

  await sql`UPDATE job_dispatches SET next_try_at = now() WHERE job_id = ${jobId}`;
  const refused = recorder(() => ({ kind: "rejected", message: "GitHub refused to start the runner (HTTP 403)." }));
  await D.drainDispatches("production", { jobId, with: refused.d });
  const job = await A.getJob(jobId);
  assert.equal(job!.status, "failed");
  assert.equal(job!.error_code, "dispatch_rejected");
  [row] = await sql<{ status: string; next_try_at: Date }[]>`SELECT status, next_try_at FROM job_dispatches WHERE job_id = ${jobId}`;
  assert.equal(row.status, "failed");
});

test("a dispatch whose sender died is sent again", { skip }, async () => {
  const { jobId } = await A.createJob({ kind: "report", reportType: "fsr_brief",
    params: { publication_id: "latest", refresh: "latest_data" }, trigger: "manual", environment: "production",
    requestedBy: "owner" });
  await sql`UPDATE job_dispatches SET status = 'sending', claimed_until = now() - interval '1 minute' WHERE job_id = ${jobId}`;
  const r = recorder();
  await D.drainDispatches("production", { jobId, with: r.d });
  assert.deepEqual(r.calls, [jobId]);
});

test("a cancelled job is never dispatched", { skip }, async () => {
  const { jobId } = await A.createJob({ kind: "report", reportType: "mpr_brief",
    params: { publication_id: "latest", refresh: "latest_data" }, trigger: "manual", environment: "production",
    requestedBy: "owner" });
  assert.equal(await A.requestCancel(jobId, "owner"), "cancelled");
  const r = recorder();
  await D.drainDispatches("production", { jobId, with: r.d });
  assert.deepEqual(r.calls, []);
});

test("an accepted dispatch that no runner claims is dispatched again by the reaper", { skip }, async () => {
  const { jobId } = await A.createJob({ kind: "report", reportType: "monthly", params: { ...monthly, period: "2026-06" },
    trigger: "manual", environment: "production", requestedBy: "owner" });
  await D.drainDispatches("production", { jobId, with: recorder().d });
  await sql`UPDATE job_dispatches SET accepted_at = now() - interval '1 hour' WHERE job_id = ${jobId}`;
  const reaped = await sql<{ job_id: string; action: string }[]>`SELECT * FROM azm_reap('production')`;
  assert.ok(reaped.some((r) => r.job_id === jobId && r.action === "redispatched"));
  const r = recorder();
  await D.drainDispatches("production", { jobId, with: r.d });
  assert.deepEqual(r.calls, [jobId]);
});

test("the tick starts each due slot once, and only when automatic checks are on", { skip }, async () => {
  const at = new Date("2026-09-24T05:16:00Z");       // 09:16 Baku
  let res = await S.tick(at);
  assert.deepEqual(res.scheduled, []);
  assert.match(res.scheduling, /turned off/);

  await A.saveSettings("production", "owner", { auto_checks_enabled: true });
  res = await S.tick(at);
  assert.equal(res.scheduled.length, 1);
  assert.equal(res.scheduled[0].created, true);
  res = await S.tick(new Date("2026-09-24T05:31:00Z"));
  assert.equal(res.scheduled.length, 1);
  assert.equal(res.scheduled[0].created, false);            // the same slot, not a second check
  const jobs = await sql<{ n: number }[]>`SELECT count(*)::int AS n FROM report_jobs WHERE kind = 'source_check'`;
  assert.equal(jobs[0].n, 1);
});

test("a preview deployment never schedules", { skip }, async () => {
  process.env.AZMONITOR_ENVIRONMENT = "preview";
  try {
    const res = await S.tick(new Date("2026-09-24T09:16:00Z"));
    assert.deepEqual(res.scheduled, []);
    assert.match(res.scheduling, /only in production/);
  } finally {
    process.env.AZMONITOR_ENVIRONMENT = "production";
  }
});

test("monitoring names a scheduled check that never started", { skip }, async () => {
  // inside the hour after 09:15 the check started by the tick test is not reported
  const soon = await S.monitor("production", new Date("2026-09-24T05:40:00Z"), true);
  assert.ok(!soon.some((p) => p.includes("source check") && p.includes("never started")), soon.join("; "));
  const later = await S.monitor("production", new Date("2026-09-25T12:00:00Z"), true);
  assert.ok(later.some((p) => p.includes("source check for 2026-09-25 13:15 never started")), later.join("; "));
});

test("the identical-edition offer holds until a productive change is detected", { skip }, async () => {
  const { publish } = await import("./helpers/db.ts");
  const editionId = await publish(sql, { edition: "2026-05", version: 1 });
  await sql`INSERT INTO current_fingerprints(environment, report_type, scope_key, fingerprint, edition_id)
            VALUES ('production', 'monthly', 'latest', 'fp1', ${editionId})`;
  const params = { period: "latest", refresh: "latest_data" } as const;
  assert.equal((await A.identicalEdition("production", "monthly", params))?.edition_id, editionId);
  assert.equal(await A.identicalEdition("production", "monthly", { ...params, refresh: "check_sources" }), null);
  await sql`INSERT INTO source_changes(change_id, batch_id, environment, classification, notifiable)
            VALUES ('chg_1', 'b1', 'production', 'new_observations', true)`;
  assert.equal(await A.identicalEdition("production", "monthly", params), null);
});

test("the throttle holds across calls", { skip }, async () => {
  const results = [];
  for (let i = 0; i < 4; i += 1) results.push(await A.withinLimit("test:bucket", 3, 3600));
  assert.deepEqual(results, [true, true, true, false]);
});
