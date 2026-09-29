import { NextResponse } from "next/server";
import { environment } from "@/lib/appstate";
import { sql } from "@/lib/db";
import { drainOutbox } from "@/lib/email/outbox";
import { handler, Refusal } from "@/lib/guard";
import { JOB_ID_PATTERN } from "@/lib/ids";
import { jobView } from "@/lib/jobview";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export const GET = handler<{ params: Promise<{ id: string }> }>(async (_request, caller, { params }) => {
  const { id } = await params;
  if (!JOB_ID_PATTERN.test(id)) throw new Refusal(404, "No such job.", "not_found");
  // Someone is watching this job: send its due email now rather than at the next scheduler tick.
  // Best effort — the tick sends it anyway, and the ledger makes a second attempt harmless.
  const due = await sql()<{ delivery_id: string }[]>`
    SELECT delivery_id FROM email_outbox
     WHERE job_id = ${id} AND environment = ${environment()} AND status = 'queued' AND not_before <= now()
     LIMIT 3`;
  for (const d of due) await drainOutbox(environment(), { deliveryId: d.delivery_id }).catch(() => undefined);
  const view = await jobView(id, caller.email);
  if (!view) throw new Refusal(404, "No such job.", "not_found");
  return NextResponse.json(view, { headers: { "cache-control": "no-store" } });
});
