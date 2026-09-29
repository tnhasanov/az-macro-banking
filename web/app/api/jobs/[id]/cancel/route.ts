import { NextResponse } from "next/server";
import { requestCancel } from "@/lib/appstate";
import { handler, Refusal } from "@/lib/guard";
import { JOB_ID_PATTERN } from "@/lib/ids";

export const runtime = "nodejs";

/** Queued jobs stop at once; a running job stops at its next stage boundary, never mid-write. */
export const POST = handler<{ params: Promise<{ id: string }> }>(async (_request, caller, { params }) => {
  const { id } = await params;
  if (!JOB_ID_PATTERN.test(id)) throw new Refusal(404, "No such job.", "not_found");
  const status = await requestCancel(id, caller.subject);
  if (!status) throw new Refusal(409, "The job has already finished.", "finished");
  return NextResponse.json({ status, note: status === "cancelled" ? "Cancelled."
    : "Cancellation requested; the worker stops at its next stage." });
}, { write: true });
