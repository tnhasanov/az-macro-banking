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
import { decide, OVERDUE_MINUTES } from "@/lib/schedule";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

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
  if (!OVERDUE_MINUTES[task]) {
    return NextResponse.json({ error: `unknown task '${task}'` }, { status: 404 });
  }

  const now = new Date();
  const [held, runs] = await Promise.all([
    locks().catch(() => []),
    lastRunPerTask().catch(() => []),
  ]);
  const last = runs.find((r) => r.task === task);
  const lastAt = last?.started_at ? new Date(last.started_at) : null;
  const decision = decide({ task, now, lastRunAt: lastAt, locks: held });

  const result: Record<string, unknown> = {
    task,
    checked_at: now.toISOString(),
    last_run_at: lastAt?.toISOString() ?? null,
    last_run_status: last?.status ?? null,
    minutes_since_last_run:
      decision.minutesSince === null ? null : Math.round(decision.minutesSince),
    overdue_after_minutes: OVERDUE_MINUTES[task],
  };

  if (decision.act !== "dispatch") {
    // `unknown-task` cannot reach here — the table was checked above — but handling it in the same
    // place as `wait` means a task added to the schedule with no threshold declines rather than
    // falling through into a dispatch.
    return NextResponse.json({
      ...result, dispatched: false,
      reason: decision.act === "wait" ? decision.reason : "no threshold is defined for this task",
      ...(decision.act === "wait" && decision.holder
        ? { holder: decision.holder, expires_at: decision.expiresAt }
        : {}),
    });
  }

  // Disabled by default. The brief is explicit that a preview must not run the engine or send
  // anything, so this has to be turned on deliberately, per deployment, and is off until then.
  if (process.env.AZMONITOR_CRON_ENABLED !== "true") {
    return NextResponse.json({
      ...result, dispatched: false,
      reason: `${decision.reason}, but dispatch is disabled in this deployment `
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
