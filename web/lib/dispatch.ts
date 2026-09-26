/**
 * Starting a runner for a job: the dispatch outbox and the one thing that drains it.
 *
 * A job is created with a pending dispatch row in the same transaction. Draining the outbox sends
 * each pending row to GitHub and records what came back; the scheduler tick drains it every few
 * minutes, and a request drains its own row immediately so a person is not kept waiting for the
 * next tick. A dispatch GitHub accepted is not a job that ran — the job is only `running` once a
 * worker has claimed it — and an accepted dispatch that is never claimed is noticed by `azm_reap`
 * and sent again.
 *
 * What the browser can influence here: nothing. The repository, workflow file and ref are fixed
 * server-side; the only input is a job id the database generated, which the workflow validates
 * again against its exact shape before using it.
 */
import { spawn } from "node:child_process";
import { mkdirSync, openSync } from "node:fs";
import path from "node:path";
import { sql } from "./db";
import { environment, type Env } from "./appstate";

export const WORKFLOW = "report-job.yml";
const REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const REF = /^[A-Za-z0-9_./-]{1,100}$/;

export type DispatchResult =
  | { kind: "accepted"; runId?: number | null; runUrl?: string | null }
  | { kind: "retry"; message: string }
  | { kind: "rejected"; message: string };

export interface Dispatcher {
  name: string;
  dispatch(jobId: string): Promise<DispatchResult>;
}

/** Which dispatcher this deployment uses, and why none if none. */
export function dispatcher(env: Env = environment()): Dispatcher | { name: "off"; reason: string } {
  const mode = process.env.AZMONITOR_DISPATCH_MODE ?? (env === "production" ? "github" : "off");
  if (mode === "local") {
    if (env === "production") {
      return { name: "off", reason: "local dispatch runs the worker on the web server and is refused in production" };
    }
    return localDispatcher();
  }
  if (mode === "github") {
    const token = process.env.GITHUB_DISPATCH_TOKEN;
    const repo = process.env.GITHUB_REPOSITORY ?? "";
    const ref = process.env.GITHUB_WORKFLOW_REF || "main";
    if (!token) return { name: "off", reason: "GITHUB_DISPATCH_TOKEN is not configured" };
    if (!REPO.test(repo)) return { name: "off", reason: "GITHUB_REPOSITORY is not configured as owner/name" };
    if (!REF.test(ref)) return { name: "off", reason: "GITHUB_WORKFLOW_REF is not a branch name" };
    return githubDispatcher(token, repo, ref);
  }
  return {
    name: "off",
    reason: env === "production"
      ? `AZMONITOR_DISPATCH_MODE=${mode} turns dispatch off`
      : "this is not the production deployment; jobs are recorded but no runner is started "
        + "(set AZMONITOR_DISPATCH_MODE to start one)",
  };
}

/**
 * GitHub's workflow_dispatch. With `return_run_details` the API answers 200 with the run it
 * created (changelog 2026-02-19); without it, or on an older API, 204 and no run id — the worker
 * records its own run id when it claims the job, so either answer is enough.
 */
export function githubDispatcher(token: string, repo: string, ref: string,
  fetcher: typeof fetch = fetch): Dispatcher {
  return {
    name: "github",
    async dispatch(jobId) {
      let response: Response;
      try {
        response = await fetcher(`https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`, {
          method: "POST",
          headers: {
            authorization: `Bearer ${token}`,
            accept: "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
            "content-type": "application/json",
          },
          body: JSON.stringify({ ref, inputs: { job_id: jobId }, return_run_details: true }),
          signal: AbortSignal.timeout(20_000),
        });
      } catch (error) {
        return { kind: "retry", message: `GitHub could not be reached (${error instanceof Error ? error.name : "error"})` };
      }
      if (response.status === 204) return { kind: "accepted" };
      if (response.status === 200) {
        const body = await response.json().catch(() => ({})) as { workflow_run_id?: number; html_url?: string };
        return { kind: "accepted", runId: body.workflow_run_id ?? null, runUrl: body.html_url ?? null };
      }
      // Only the status is kept: GitHub's error body can echo the repository path or request.
      if ([401, 403, 404, 422].includes(response.status)) {
        return {
          kind: "rejected",
          message: `GitHub refused to start the runner (HTTP ${response.status}). Check that the dispatch token `
            + `can run ${WORKFLOW} in this repository and that the workflow exists on the ${ref} branch.`,
        };
      }
      return { kind: "retry", message: `GitHub answered HTTP ${response.status}; the dispatch will be retried` };
    },
  };
}

/** Development only: run the worker on this machine, as the runner would. */
function localDispatcher(): Dispatcher {
  return {
    name: "local",
    async dispatch(jobId) {
      const root = path.resolve(process.cwd(), "..");
      const python = process.env.AZMONITOR_PYTHON || "python3";
      const logs = path.join(process.env.AZMONITOR_DATA_DIR ?? path.join(root, "data"), "..", "logs");
      mkdirSync(logs, { recursive: true });
      const log = openSync(path.join(logs, `${jobId}.log`), "a");
      const child = spawn(python, ["-m", "azmonitor.jobs", "run", "--job-id", jobId], {
        cwd: root, detached: true, stdio: ["ignore", log, log], env: process.env,
      });
      child.unref();
      return { kind: "accepted", runId: null, runUrl: null };
    },
  };
}

const BACKOFF_SECONDS = [30, 120, 480, 1800, 3600];
const MAX_TRIES = 6;

/**
 * Send due dispatches. Rows are taken with SKIP LOCKED, so two ticks (or a tick and a request)
 * never send the same row. A row left `sending` by an invocation that died is sent again: the worst
 * case is a second runner, which finds the job already claimed and exits without touching it.
 */
export async function drainDispatches(env: Env, opts: { jobId?: string; limit?: number; with?: Dispatcher } = {}) {
  const d = opts.with ?? dispatcher(env);
  if (!("dispatch" in d)) return { dispatcher: "off", reason: d.reason, sent: 0, results: [] };

  await sql()`UPDATE job_dispatches SET status = 'pending',
                     last_error = 'the previous attempt to dispatch did not report back; sending again'
               WHERE status = 'sending' AND claimed_until < now()`;
  const jobFilter = opts.jobId ?? null;
  const rows = await sql()<{ dispatch_id: string; job_id: string; attempt: number; tries: number }[]>`
    UPDATE job_dispatches d SET status = 'sending', claimed_until = now() + interval '2 minutes', tries = d.tries + 1
     WHERE d.dispatch_id IN (
       SELECT d2.dispatch_id FROM job_dispatches d2 JOIN report_jobs j ON j.job_id = d2.job_id
        WHERE d2.status = 'pending' AND d2.next_try_at <= now() AND j.environment = ${env}
          AND j.status = 'queued' AND NOT j.cancel_requested
          AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= now())
          AND (${jobFilter}::text IS NULL OR d2.job_id = ${jobFilter}::text)
        ORDER BY d2.created_at LIMIT ${opts.limit ?? 5}
        FOR UPDATE OF d2 SKIP LOCKED)
    RETURNING d.dispatch_id, d.job_id, d.attempt, d.tries`;

  const results: { job_id: string; outcome: string; message?: string }[] = [];
  for (const row of rows) {
    const res = await d.dispatch(row.job_id);
    if (res.kind === "accepted") {
      await sql().begin(async (tx) => {
        await tx`UPDATE job_dispatches SET status = 'accepted', accepted_at = now(), claimed_until = NULL,
                        run_id = ${res.runId ?? null}, run_url = ${res.runUrl ?? null}, last_error = NULL
                  WHERE dispatch_id = ${row.dispatch_id}`;
        await tx`UPDATE report_jobs SET status = 'dispatched', stage_detail = 'waiting for a runner to start',
                        run_id = coalesce(${res.runId ?? null}, run_id), run_url = coalesce(${res.runUrl ?? null}, run_url)
                  WHERE job_id = ${row.job_id} AND status = 'queued'`;
        await tx`INSERT INTO job_events(job_id, attempt, status, message)
                 SELECT job_id, attempt, 'dispatched', ${`runner requested via ${d.name}`} FROM report_jobs
                  WHERE job_id = ${row.job_id}`;
      });
      results.push({ job_id: row.job_id, outcome: "accepted" });
    } else if (res.kind === "retry" && row.tries < MAX_TRIES) {
      const delay = BACKOFF_SECONDS[Math.min(row.tries - 1, BACKOFF_SECONDS.length - 1)];
      await sql()`UPDATE job_dispatches SET status = 'pending', claimed_until = NULL, last_error = ${res.message},
                         next_try_at = now() + make_interval(secs => ${delay})
                   WHERE dispatch_id = ${row.dispatch_id}`;
      await sql()`UPDATE report_jobs SET stage_detail = ${`${res.message} (next attempt in ${Math.round(delay / 60) || 1} min)`}
                   WHERE job_id = ${row.job_id} AND status = 'queued'`;
      results.push({ job_id: row.job_id, outcome: "retry", message: res.message });
    } else {
      const message = res.kind === "rejected" ? res.message
        : `The runner could not be started after ${row.tries} attempts: ${res.message}`;
      await sql().begin(async (tx) => {
        await tx`UPDATE job_dispatches SET status = 'failed', claimed_until = NULL, last_error = ${message}
                  WHERE dispatch_id = ${row.dispatch_id}`;
        await tx`UPDATE report_jobs SET status = 'failed', finished_at = now(), fence = fence + 1,
                        error_code = ${res.kind === "rejected" ? "dispatch_rejected" : "dispatch_unavailable"},
                        error_message = ${message}
                  WHERE job_id = ${row.job_id} AND status = 'queued'`;
        await tx`INSERT INTO job_events(job_id, attempt, status, message)
                 SELECT job_id, attempt, 'failed', ${message} FROM report_jobs WHERE job_id = ${row.job_id}`;
      });
      results.push({ job_id: row.job_id, outcome: "failed", message });
    }
  }
  return { dispatcher: d.name, sent: rows.length, results };
}
