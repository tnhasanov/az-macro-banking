/**
 * The scheduled trigger — a watchdog, not a second scheduler.
 *
 * The engine runs on a GitHub Actions runner, because it needs LibreOffice and about twelve minutes
 * (see docs/vercel-architecture.md). Actions has its own cron, and that is the primary trigger. If
 * this endpoint also fired the workflow on a schedule, every task would be dispatched twice and the
 * second run would exist only to lose a race for the lease.
 *
 * So it does something more useful with the same cron slot: it checks whether the run that should
 * have happened actually happened, and dispatches one only if it did not. That covers the ways
 * GitHub's own cron quietly stops — it is best-effort under load, and Actions disables scheduled
 * workflows in a repository with no activity for sixty days — without ever duplicating a run that
 * is already under way.
 *
 * Three things make repeated or concurrent invocations safe, and all three are needed because cron
 * delivery is at-least-once everywhere:
 *
 *   1. this endpoint dispatches only when the last successful run is genuinely overdue;
 *   2. it declines while a lease is held, so a run in progress is never joined;
 *   3. the workflow itself has a concurrency group, and the engine takes a database lease, so even
 *      two dispatches that slip through produce one run and one skip.
 */
import { NextResponse } from "next/server";
import { authorizeScheduler } from "@/lib/auth";
import { lastRunPerTask, locks } from "@/lib/db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * How long after its scheduled time a task is considered missed.
 *
 * Measured from the last successful run, in minutes, and set to just over the longest gap between
 * two scheduled runs of that task plus the time one takes. A shorter window would dispatch a
 * duplicate every time a run was merely slow.
 */
const OVERDUE_MINUTES: Record<string, number> = {
  // 09:15, 13:15, 17:15 Baku — longest gap is the overnight one, sixteen hours.
  "source-check": 16 * 60 + 90,
  // Monday 08:30 Baku.
  "weekly-digest": 7 * 24 * 60 + 120,
  // 07:45 and 18:45 Baku — longest gap thirteen hours.
  monitor: 13 * 60 + 90,
};

export async function GET(
  request: Request,
  { params }: { params: Promise<{ task: string }> },
) {
  const auth = authorizeScheduler(request);
  if (!auth.ok) {
    // 401 rather than 404: a scheduler that is misconfigured should see that it was refused.
    return NextResponse.json({ error: "unauthorised", reason: auth.reason }, { status: 401 });
  }

  const { task } = await params;
  const overdueAfter = OVERDUE_MINUTES[task];
  if (!overdueAfter) {
    return NextResponse.json({ error: `unknown task '${task}'` }, { status: 404 });
  }

  const now = new Date();
  const result: Record<string, unknown> = { task, checked_at: now.toISOString() };

  // A run in progress owns the dataset. Nothing is dispatched while the lease is held.
  const held = await locks().catch(() => []);
  const active = held.filter((l) => !l.expired);
  if (active.length > 0) {
    return NextResponse.json({
      ...result, dispatched: false,
      reason: "a run holds the lease", holder: active[0].holder, expires_at: active[0].expires_at,
    });
  }

  const runs = await lastRunPerTask().catch(() => []);
  const last = runs.find((r) => r.task === task);
  const lastAt = last?.started_at ? new Date(last.started_at) : null;
  const minutesSince = lastAt ? (now.getTime() - lastAt.getTime()) / 60000 : Infinity;
  result.last_run_at = lastAt?.toISOString() ?? null;
  result.last_run_status = last?.status ?? null;
  result.minutes_since_last_run = Number.isFinite(minutesSince) ? Math.round(minutesSince) : null;
  result.overdue_after_minutes = overdueAfter;

  if (minutesSince < overdueAfter) {
    return NextResponse.json({ ...result, dispatched: false, reason: "the scheduled run is on time" });
  }

  // Disabled by default. The brief is explicit that a preview must not run the engine or send
  // anything, so this has to be turned on deliberately, per deployment, and is off until then.
  if (process.env.AZMONITOR_CRON_ENABLED !== "true") {
    return NextResponse.json({
      ...result, dispatched: false,
      reason: "the run is overdue, but dispatch is disabled in this deployment "
        + "(set AZMONITOR_CRON_ENABLED=true to allow it)",
    });
  }

  const dispatch = await dispatchWorkflow(task);
  return NextResponse.json({ ...result, dispatched: dispatch.ok, ...dispatch },
    { status: dispatch.ok ? 202 : 502 });
}

/** Ask GitHub to run the engine workflow once. */
async function dispatchWorkflow(task: string): Promise<{ ok: boolean; reason?: string }> {
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  const repo = process.env.GITHUB_REPOSITORY;
  const ref = process.env.GITHUB_WORKFLOW_REF || "main";
  if (!token || !repo) {
    return { ok: false, reason: "GITHUB_DISPATCH_TOKEN or GITHUB_REPOSITORY is not configured" };
  }

  const response = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/scheduled.yml/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref, inputs: { task, trigger: "vercel-watchdog" } }),
    },
  );

  if (response.status === 204) return { ok: true };
  // The body can carry a token or a repository path, so only the status is reported back.
  return { ok: false, reason: `GitHub refused the dispatch with status ${response.status}` };
}
