import { NextResponse } from "next/server";
import { createJob, environment, getJob } from "@/lib/appstate";
import { sql } from "@/lib/db";
import { drainDispatches } from "@/lib/dispatch";
import { handler, Refusal, throttle } from "@/lib/guard";
import { JOB_ID_PATTERN } from "@/lib/ids";

export const runtime = "nodejs";

/** A retry is a new job with the same request, so the failed attempt's record stays as it was. */
export const POST = handler<{ params: Promise<{ id: string }> }>(async (_request, caller, { params }) => {
  await throttle(caller, "create_job", 30, 3600);
  const { id } = await params;
  if (!JOB_ID_PATTERN.test(id)) throw new Refusal(404, "No such job.", "not_found");
  const old = await getJob(id);
  if (!old) throw new Refusal(404, "No such job.", "not_found");
  if (!["failed", "blocked", "cancelled", "waiting_for_data"].includes(old.status)) {
    throw new Refusal(409, "Only a job that failed, was blocked, cancelled or is waiting for data can be retried.",
      "not_retryable");
  }
  const env = environment();
  if (old.environment !== env) throw new Refusal(404, "No such job.", "not_found");
  const { jobId, created } = await createJob({
    kind: old.kind as "report" | "source_check" | "weekly_digest", reportType: old.report_type, params: old.params,
    trigger: "retry", environment: env, requestedBy: caller.subject,
    requesterEmail: old.notify_requester ? old.requester_email : null, notifyRequester: old.notify_requester,
  });
  if (created) {
    await sql()`INSERT INTO job_events(job_id, attempt, status, message) VALUES (${jobId}, 0, 'queued', ${`retry of ${id}`})`;
    await sql()`INSERT INTO job_events(job_id, attempt, status, message)
                VALUES (${id}, ${old.attempt}, ${old.status}, ${`retried as ${jobId} by ${caller.subject}`})`;
    await drainDispatches(env, { jobId }).catch(() => undefined);
  }
  return NextResponse.json({ job_id: jobId, created, href: `/jobs/${jobId}` }, { status: created ? 201 : 200 });
}, { write: true });
