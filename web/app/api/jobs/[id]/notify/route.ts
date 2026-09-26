import { NextResponse } from "next/server";
import { environment, getJob } from "@/lib/appstate";
import { sql } from "@/lib/db";
import { queueCopy, drainOutbox } from "@/lib/email/outbox";
import { handler, Refusal, throttle } from "@/lib/guard";
import { JOB_ID_PATTERN } from "@/lib/ids";

export const runtime = "nodejs";

/**
 * "Email me when it is ready", after the fact. On a job still in progress the worker reads the
 * request at publication time; on a finished job with an edition the copy is queued now.
 */
export const POST = handler<{ params: Promise<{ id: string }> }>(async (_request, caller, { params }) => {
  await throttle(caller, "email", 20, 3600);
  const { id } = await params;
  if (!JOB_ID_PATTERN.test(id)) throw new Refusal(404, "No such job.", "not_found");
  if (!caller.email) throw new Refusal(400, "This sign-in has no email address configured.", "no_email");
  const job = await getJob(id);
  if (!job || job.environment !== environment()) throw new Refusal(404, "No such job.", "not_found");
  const updated = await sql()`UPDATE report_jobs SET notify_requester = true,
                                     requester_email = coalesce(requester_email, ${caller.email})
                               WHERE job_id = ${id} AND status IN ('queued','dispatched','running')
                               RETURNING job_id`;
  if (updated.length) return NextResponse.json({ note: "You will be emailed when this job finishes publishing." });
  if (job.edition_id) {
    const deliveryId = await queueCopy(environment(), job.edition_id, caller.email, "manual_request", id);
    await drainOutbox(environment(), { deliveryId }).catch(() => undefined);
    return NextResponse.json({ note: "The report has been queued to be emailed to you.", delivery_id: deliveryId });
  }
  throw new Refusal(409, "This job finished without an edition, so there is nothing to email.", "no_edition");
}, { write: true });
