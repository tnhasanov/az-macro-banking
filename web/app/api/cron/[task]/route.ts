/**
 * The scheduled trigger — a watchdog, not a second scheduler.
 *
 * The engine runs on a GitHub Actions runner, because it needs LibreOffice and about twelve minutes
 * (see docs/vercel-architecture.md). Actions has its own cron, and that is the primary trigger. If
 * this endpoint also fired the workflow on a schedule, every task would be dispatched twice and the
 * second run would exist only to lose a race for the lease.
 *
 * So it does something more useful with the same cron slot: it works out which scheduled runs
 * should have happened, and dispatches one only where nothing did. That covers the ways GitHub's
 * own cron quietly stops — it is best-effort under load, and Actions disables scheduled workflows
 * in a repository with no activity for sixty days.
 *
 * **It fails closed.** A database query that fails is not evidence that nothing is running; it is
 * evidence that we cannot tell. Reading "no lease held" out of a failed query is how a watchdog
 * starts a second worker on top of a healthy one during a Neon outage. Every read here either
 * succeeds or stops the request with a 503.
 */
import { NextResponse } from "next/server";
import { authorizeScheduler } from "@/lib/auth";
import { recentRuns, locks } from "@/lib/db";
import { describe, evaluate, SCHEDULE, shouldDispatch } from "@/lib/schedule";

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
  if (!SCHEDULE[task]) {
    return NextResponse.json({ error: `unknown task '${task}'` }, { status: 404 });
  }

  const now = new Date();

  // Both reads are required. If either fails, this request ends here.
  let runs, held;
  try {
    [runs, held] = await Promise.all([recentRuns(task, 12), locks()]);
  } catch (error) {
    // Deliberately not caught into an empty array. An outage must not read as "nothing is running".
    return NextResponse.json(
      {
        task,
        checked_at: now.toISOString(),
        dispatched: false,
        error: "the run history could not be read, so this check cannot tell whether a run is "
          + "under way; nothing has been dispatched",
        detail: error instanceof Error ? error.message : String(error),
      },
      { status: 503 },
    );
  }

  const verdict = evaluate({ task, now, runs, locks: held });
  const result: Record<string, unknown> = {
    task,
    checked_at: now.toISOString(),
    state: verdict.state,
    reason: describe(verdict),
    occurrence: "occurrence" in verdict && verdict.occurrence
      ? { due: verdict.occurrence.due.toISOString(), label: verdict.occurrence.label,
          grace_minutes: verdict.occurrence.graceMinutes }
      : null,
  };

  if (!shouldDispatch(verdict)) {
    return NextResponse.json({ ...result, dispatched: false });
  }

  // Disabled by default. The brief is explicit that a preview must not run the engine or send
  // anything, so this has to be turned on deliberately, per deployment, and is off until then.
  if (process.env.AZMONITOR_CRON_ENABLED !== "true") {
    return NextResponse.json({
      ...result, dispatched: false,
      reason: `${result.reason}, but dispatch is disabled in this deployment `
        + "(set AZMONITOR_CRON_ENABLED=true to allow it)",
    });
  }

  const dispatch = await dispatchWorkflow(task);
  return NextResponse.json({ ...result, dispatched: dispatch.ok, ...dispatch },
    { status: dispatch.ok ? 202 : 502 });
}

/** Ask GitHub to run the engine workflow once. */
async function dispatchWorkflow(task: string): Promise<{ ok: boolean; detail?: string }> {
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  const repo = process.env.GITHUB_REPOSITORY;
  const ref = process.env.GITHUB_WORKFLOW_REF || "main";
  if (!token || !repo) {
    return { ok: false, detail: "GITHUB_DISPATCH_TOKEN or GITHUB_REPOSITORY is not configured" };
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
  return { ok: false, detail: `GitHub refused the dispatch with status ${response.status}` };
}
