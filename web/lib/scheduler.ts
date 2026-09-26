/**
 * The one scheduler: a Vercel cron invocation every fifteen minutes that does all of the system's
 * timed work, in this order.
 *
 *   1. start the source checks and the weekly digest that are due, once each (a unique slot key
 *      in the database, not the cron, is what guarantees "once");
 *   2. reclaim jobs whose worker stopped renewing its lease, and re-dispatch jobs GitHub accepted
 *      but never started (`azm_reap`);
 *   3. send pending dispatches;
 *   4. send due email;
 *   5. look at the system and record what needs a person.
 *
 * GitHub Actions has no schedule of its own any more. Two schedulers for the same work is how a
 * run happens twice or, when each assumes the other covered it, not at all.
 */
import { sql } from "./db";
import { createJob, ensureSchema, environment, notificationSettings, recipients, type Env } from "./appstate";
import { drainDispatches } from "./dispatch";
import { drainOutbox, queueAlert } from "./email/outbox";
import { dueSlots, lastOccurrences } from "./slots";

export interface TickResult {
  at: string;
  environment: Env;
  scheduled: { slot: string; job_id: string; created: boolean }[];
  scheduling: string;
  reaped: { job_id: string; action: string }[];
  dispatch: unknown;
  email: unknown;
  problems: string[];
  alerted: boolean;
}

export async function tick(now = new Date()): Promise<TickResult> {
  await ensureSchema();
  const env = environment();
  const settings = await notificationSettings(env);
  const result: TickResult = {
    at: now.toISOString(), environment: env, scheduled: [], scheduling: "", reaped: [], dispatch: null,
    email: null, problems: [], alerted: false,
  };

  if (env !== "production") {
    result.scheduling = `automatic checks run only in production; this is ${env}`;
  } else if (!settings.auto_checks_enabled) {
    result.scheduling = "automatic source checks are turned off in Settings";
  } else {
    for (const slot of dueSlots(now)) {
      const { jobId, created } = await createJob({
        kind: slot.kind, reportType: slot.kind === "weekly_digest" ? "weekly" : null,
        params: slot.kind === "weekly_digest" ? { period: "latest", refresh: "latest_data" } : {},
        trigger: "schedule", environment: env, requestedBy: "scheduler", scheduleSlot: slot.key,
      });
      result.scheduled.push({ slot: slot.key, job_id: jobId, created });
    }
    result.scheduling = "automatic source checks are on";
  }

  result.reaped = await sql()<{ job_id: string; action: string }[]>`SELECT * FROM azm_reap(${env})`;
  result.dispatch = await drainDispatches(env, { limit: 10 });
  result.email = await drainOutbox(env, { limit: 25 });
  const health = await monitor(env, now, settings.auto_checks_enabled);
  result.problems = health;
  result.alerted = await alertIfNew(env, health);

  await sql()`INSERT INTO meta(key, value, updated_at) VALUES ('scheduler_tick', ${sql().json(result as never)}, now())
              ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`.catch(() => undefined);
  return result;
}

/** What needs a person. Each line says what is wrong and where to look. */
export async function monitor(env: Env, now: Date, checksOn: boolean): Promise<string[]> {
  const problems: string[] = [];
  const [counts] = await sql()<{ stuck: number; failed: number; blocked: number; queued_long: number }[]>`
    SELECT count(*) FILTER (WHERE status = 'running' AND heartbeat_at < now() - interval '10 minutes')::int AS stuck,
           count(*) FILTER (WHERE status = 'failed' AND finished_at > now() - interval '24 hours')::int AS failed,
           count(*) FILTER (WHERE status = 'blocked' AND finished_at > now() - interval '24 hours')::int AS blocked,
           count(*) FILTER (WHERE status IN ('queued','dispatched') AND requested_at < now() - interval '45 minutes')::int AS queued_long
      FROM report_jobs WHERE environment = ${env}`;
  if (counts.stuck) problems.push(`${counts.stuck} running job(s) have not reported progress for 10 minutes`);
  if (counts.failed) problems.push(`${counts.failed} job(s) failed in the last 24 hours`);
  if (counts.blocked) problems.push(`${counts.blocked} edition(s) were blocked by validation or quality rules in the last 24 hours`);
  if (counts.queued_long) problems.push(`${counts.queued_long} job(s) have waited over 45 minutes for a runner`);

  const [mail] = await sql()<{ uncertain: number; failed: number; waiting: number }[]>`
    SELECT count(*) FILTER (WHERE status = 'uncertain')::int AS uncertain,
           count(*) FILTER (WHERE status = 'failed' AND updated_at > now() - interval '24 hours')::int AS failed,
           count(*) FILTER (WHERE status = 'queued' AND created_at < now() - interval '2 hours')::int AS waiting
      FROM email_outbox WHERE environment = ${env} AND purpose <> 'alert'`;
  if (mail.uncertain) problems.push(`${mail.uncertain} email(s) may or may not have been sent and need resolving in Settings`);
  if (mail.failed) problems.push(`${mail.failed} email(s) failed in the last 24 hours`);
  if (mail.waiting) problems.push(`${mail.waiting} email(s) have been queued for over two hours`);

  if (checksOn && env === "production") {
    for (const slot of lastOccurrences(now)) {
      if (now.getTime() - slot.dueUtc.getTime() < 60 * 60_000) continue;     // give it an hour
      const rows = await sql()<{ status: string }[]>`
        SELECT status FROM report_jobs WHERE schedule_slot = ${slot.key}`;
      if (!rows[0]) problems.push(`the scheduled ${slot.kind.replace("_", " ")} for ${slot.label} never started`);
      else if (["failed", "cancelled"].includes(rows[0].status)) {
        problems.push(`the scheduled ${slot.kind.replace("_", " ")} for ${slot.label} ended ${rows[0].status}`);
      }
    }
  }
  const suppressed = (await recipients()).filter((r) => r.suppressed_at && r.active && !r.unsubscribed_at);
  if (suppressed.length) problems.push(`${suppressed.length} recipient address(es) are suppressed after delivery failures`);
  return problems;
}

/** One alert email to the owner per distinct set of problems per day; nothing when all is well. */
async function alertIfNew(env: Env, problems: string[]): Promise<boolean> {
  if (!problems.length || env !== "production") return false;
  const signature = problems.map((p) => p.replace(/\d+/g, "#")).sort().join("|");
  const [recent] = await sql()<{ n: number }[]>`
    SELECT count(*)::int AS n FROM audit_log
     WHERE action = 'alert_sent' AND reason = ${signature} AND at > now() - interval '24 hours'`;
  if (recent.n > 0) return false;
  const owners = (await recipients()).filter((r) => r.role === "owner" && r.active && !r.suppressed_at);
  for (const o of owners) {
    await queueAlert(env, o.email, "[azmonitor] Something needs attention",
      `The scheduler found:\n\n${problems.map((p) => `- ${p}`).join("\n")}\n\nOpen the System page of the dashboard for details.`);
  }
  await sql()`INSERT INTO audit_log(environment, actor, action, reason, detail)
              VALUES (${env}, 'scheduler', 'alert_sent', ${signature}, ${sql().json({ problems } as never)})`;
  return owners.length > 0;
}
